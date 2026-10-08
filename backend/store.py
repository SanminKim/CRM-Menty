"""Чтение и запись документов с проверкой прав.

Правила доступа:
- администратор читает и пишет всё;
- ментор читает и правит только своих студентов и связанные с ними встречи, читает потоки, команду
  и названия партнёров; выплаты, расходы и условия направлений ему недоступны;
- ментор и партнёр читают отчёт о своих начислениях и выплатах, каждый только свой.
"""
import json
import re

from django.db import DataError, transaction

from . import reports
from .models import COLLECTIONS, Account, Doc, State, Tombstone

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_DEPTH = 12
MAX_DOC_BYTES = 512 * 1024
MENTOR_READ = ("students", "cohorts", "team", "partners", "meetings", "config")
# Ментору нужны название и доля партнёра (она фиксируется в заявке), но не его контакты и реквизиты
PARTNER_PUBLIC_FIELDS = ("name", "promo", "utm", "share", "active", "demo")


class Denied(Exception):
    """Действие не разрешено правами пользователя."""


class NotFound(Exception):
    pass


class Invalid(Exception):
    pass


def role_of(user):
    """(роль, id связанной записи) или (None, "") для пользователя без доступа."""
    if not user or not user.is_authenticated or not user.is_active:
        return None, ""
    acc = getattr(user, "account", None)
    if acc is not None:
        return acc.role, acc.link_id
    if user.is_superuser:
        return Account.Role.ADMIN, ""
    return None, ""


def user_key(user):
    return f"u{user.pk}"


def merge(dst, patch):
    """Как в базе страницы: объекты сливаются по ключам, массивы и null заменяют поле целиком."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            merge(dst[key], value)
        else:
            dst[key] = value
    return dst


class Access:
    """Права одного пользователя. Создаётся на запрос."""

    def __init__(self, user):
        self.user = user
        self.role, self.link_id = role_of(user)
        self._mine = None

    @property
    def is_admin(self):
        return self.role == Account.Role.ADMIN

    @property
    def is_mentor(self):
        return self.role == Account.Role.MENTOR

    def _owns_student(self, student_id):
        if not student_id or not self.link_id:
            return False
        if self._mine is None:
            self._mine = set(Doc.objects.filter(collection="students", data__mentorId=self.link_id)
                             .values_list("doc_id", flat=True))
        return student_id in self._mine

    def can_read(self, collection, data):
        if self.is_admin:
            return True
        if self.role == Account.Role.PARTNER:
            return False  # отчёт партнёра проверяется по идентификатору, см. visible()
        if not self.is_mentor or collection not in MENTOR_READ:
            return False
        if collection == "students":
            return bool(self.link_id) and data.get("mentorId") == self.link_id
        if collection == "meetings":
            if data.get("studentId"):
                return self._owns_student(data["studentId"]) or (
                    bool(self.link_id) and data.get("mentorId") == self.link_id)
            return True  # занятия группы видят все менторы
        return True

    def visible(self, doc):
        """Документ в том виде, в каком его можно показать пользователю, или None."""
        if doc.collection == "reports" and not self.is_admin:
            # Каждый видит только свой отчёт: в нём нет ни чужих ставок, ни чужих выплат. Вид отчёта сверяется с ролью:
            # если у ментора и партнёра совпали идентификаторы, чужой отчёт не откроется
            kind = {Account.Role.PARTNER: "partner", Account.Role.MENTOR: "mentor"}.get(self.role)
            mine = bool(kind) and bool(self.link_id) and doc.doc_id == self.link_id and doc.data.get("kind", "partner") == kind  # у отчётов прежнего вида поля kind нет
            return dict(doc.data) if mine else None
        if self.role == Account.Role.PARTNER:
            return None
        if not self.can_read(doc.collection, doc.data):
            return None
        data = dict(doc.data)
        if doc.collection == "partners" and not self.is_admin:
            data = {k: v for k, v in data.items() if k in PARTNER_PUBLIC_FIELDS}
        if doc.collection == "team" and not self.is_admin:
            data.pop("share", None)  # ставка из прежней схемы расчёта: менторам она не нужна
        return data

    def check_write(self, collection, old, new):
        """old — прежние данные или None, new — новые данные или None (удаление)."""
        if self.is_admin:
            return
        if not self.is_mentor:
            raise Denied
        if collection == "students":
            if new is None:
                raise Denied  # насовсем удаляет только администратор
            if old is not None and not self.can_read(collection, old):
                raise Denied
            if not self.link_id or new.get("mentorId") != self.link_id:
                raise Denied  # ментор не может забрать чужого студента или отдать своего
            self._check_partner_fields(old, new)
            self._check_proofs(old, new)
            return
        if collection == "meetings":
            if new is not None and (old or {}).get("rsvp") != new.get("rsvp"):
                raise Denied  # ответы студентов «буду / не буду» записывает только бот
            for data in (old, new):
                if data is None:
                    continue
                own = bool(self.link_id) and data.get("mentorId") == self.link_id
                if data.get("studentId"):
                    # встреча со студентом: студент должен быть своим, а проводить её может только сам ментор
                    if not self._owns_student(data["studentId"]) or not (own or not data.get("mentorId")):
                        raise Denied
                elif not own:
                    raise Denied  # занятие группы правит тот, кто его проводит
            return
        raise Denied

    @staticmethod
    def _check_proofs(old, new):
        """Файлы от студента в карточку добавляет только бот: ментор может убрать запись, но не вписать свою."""
        def ids(data):
            proofs = (data or {}).get("proofs")
            return {p.get("fileId") for p in proofs if isinstance(p, dict)} if isinstance(proofs, list) else set()
        try:
            extra = ids(new) - ids(old)
        except TypeError:
            raise Denied  # вместо идентификатора файла что-то несуразное
        if extra:
            raise Denied

    def _check_partner_fields(self, old, new):
        """От источника и доли партнёра зависят выплаты, поэтому ментор их не меняет."""
        if old is not None:
            for key in ("partnerId", "partnerShare", "demo"):
                if (old.get(key) or None) != (new.get(key) or None):
                    raise Denied
            return
        partner_id = new.get("partnerId")
        share = 0
        if partner_id:
            partner = Doc.objects.filter(collection="partners", doc_id=str(partner_id)).first()
            if partner is None:
                raise Denied
            share = partner.data.get("share") or 0
        if (new.get("partnerShare") or 0) != share:
            raise Denied  # доля в новой заявке берётся из записи партнёра


def _team_links():
    """id записи команды → ключ пользователя, который с ней связан."""
    return {
        a.link_id: user_key(a.user)
        for a in Account.objects.select_related("user").exclude(role=Account.Role.PARTNER).exclude(link_id="")
        if a.user.is_active
    }


def present(doc, data, links):
    """Документ для отправки на страницу: идентификатор и ревизия отдельно от данных."""
    data = dict(data)
    if doc.collection == "team":
        data["userId"] = links.get(doc.doc_id)
    return {"id": doc.doc_id, "rev": doc.rev, "data": data}


def state():
    obj, _ = State.objects.get_or_create(pk=1)
    return obj


def snapshot(access, since=None, epoch=None):
    """Всё видимое пользователю (since=None) или изменения после ревизии since."""
    st = state()
    links = _team_links()
    full = since is None or epoch != st.epoch or since > st.rev
    out = {"rev": st.rev, "epoch": st.epoch, "full": full}
    docs = Doc.objects.all() if full else Doc.objects.filter(rev__gt=since)
    # Об исчезнувших записях сообщаем только в тех коллекциях, которые роль вообще может читать.
    # Партнёру — только про его собственный отчёт: чужие идентификаторы ему знать незачем.
    readable = COLLECTIONS if access.is_admin else (*MENTOR_READ, "reports") if access.is_mentor else ("reports",)
    is_partner = access.role == Account.Role.PARTNER

    def may_hear(collection, doc_id):
        if collection == "reports" and not access.is_admin:
            return doc_id == access.link_id  # об исчезновении чужого отчёта не сообщаем
        return collection in readable and not is_partner
    changed, removed = {}, {}
    for doc in docs.order_by("collection", "doc_id"):
        data = access.visible(doc)
        if data is not None:
            changed.setdefault(doc.collection, []).append(present(doc, data, links))
        elif not full and may_hear(doc.collection, doc.doc_id):
            removed.setdefault(doc.collection, []).append(doc.doc_id)
    if not full:
        for t in Tombstone.objects.filter(rev__gt=since, collection__in=readable):
            if may_hear(t.collection, t.doc_id):
                removed.setdefault(t.collection, []).append(t.doc_id)
    out["docs"] = changed
    if not full:
        out["removed"] = removed
    return out


def _depth_ok(value, left=MAX_DEPTH):
    if left < 0:
        return False
    if isinstance(value, dict):
        return all(_depth_ok(v, left - 1) for v in value.values())
    if isinstance(value, list):
        return all(_depth_ok(v, left - 1) for v in value)
    return True


def _validate(collection, doc_id, data=None):
    if collection not in COLLECTIONS:
        raise Invalid("Неизвестная коллекция")
    if not ID_RE.fullmatch(doc_id or ""):
        raise Invalid("Недопустимый идентификатор")
    if collection == "config" and doc_id != "main":
        raise Invalid("Недопустимый идентификатор")
    if data is not None:
        if not isinstance(data, dict):
            raise Invalid("Ожидается объект")
        if not _depth_ok(data):
            raise Invalid("Слишком глубокая вложенность")
        dumped = json.dumps(data, ensure_ascii=True)
        if len(dumped) > MAX_DOC_BYTES * 2 or len(json.dumps(data, ensure_ascii=False).encode()) > MAX_DOC_BYTES:
            raise Invalid("Запись слишком большая")
        if "\\u0000" in dumped:
            raise Invalid("Недопустимый символ в тексте")  # PostgreSQL не хранит нулевой символ в JSON


def lock():
    """Блокирует состояние базы до конца транзакции: записи идут строго по очереди, ревизии — по порядку."""
    State.objects.get_or_create(pk=1)
    return State.objects.select_for_update().get(pk=1)


def _next_rev():
    st = lock()
    st.rev += 1
    st.save(update_fields=["rev"])
    return st.rev


def _clean(collection, data):
    data = dict(data)
    data.pop("id", None)
    if collection == "team":
        data.pop("userId", None)  # связь с учётной записью хранится в Account
    return data


def _touch_meetings(student_id, rev):
    """Видимость встречи зависит от ментора студента: при его смене встречи должны «обновиться»."""
    Doc.objects.filter(collection="meetings", data__studentId=student_id).update(rev=rev)


@transaction.atomic
def write(access, collection, doc_id, data, partial):
    """set (partial=False) или update (partial=True). Возвращает (ревизия, документ для пользователя)."""
    _validate(collection, doc_id, data)
    if access.role not in (Account.Role.ADMIN, Account.Role.MENTOR):
        raise Denied
    rev = _next_rev()
    doc = Doc.objects.select_for_update().filter(collection=collection, doc_id=doc_id).first()
    if partial and doc is None:
        if not access.is_admin:
            raise Denied  # не подсказываем, существует ли запись
        raise NotFound
    old = doc.data if doc else None
    patch = _clean(collection, data)
    new = merge(json.loads(json.dumps(old)), patch) if partial else patch
    _validate(collection, doc_id, new)
    access.check_write(collection, old, new)
    if doc is None:
        doc = Doc(collection=collection, doc_id=doc_id)
        Tombstone.objects.filter(collection=collection, doc_id=doc_id).delete()
    if collection == "reports":
        # Отчёт считает сервер: то, что прислала страница, служит только просьбой открыть отчёт
        new = reports.compute(doc_id)
        if new is None:
            raise Invalid("Партнёр или ментор не найден")
    doc.data, doc.rev, doc.updated_by = new, rev, access.user
    _save(doc)
    if collection == "students" and old is not None and old.get("mentorId") != new.get("mentorId"):
        _touch_meetings(doc_id, rev)
        if isinstance(new.get("mentorId"), str) and new["mentorId"] and not new.get("deletedAt"):
            _tell_mentor(new["mentorId"], str(new.get("name") or "")[:100], access.user, doc_id)
    _refresh_reports(collection, rev, old, new)
    visible = access.visible(doc)
    return rev, present(doc, visible, _team_links()) if visible is not None else None


@transaction.atomic
def delete(access, collection, doc_id):
    _validate(collection, doc_id)
    if not access.is_admin and not access.is_mentor:
        raise Denied
    st = lock()
    doc = Doc.objects.select_for_update().filter(collection=collection, doc_id=doc_id).first()
    if doc is None:
        if not access.is_admin:
            raise Denied
        return st.rev  # удалять нечего: ревизия не меняется
    access.check_write(collection, doc.data, None)
    rev = _next_rev()
    old = doc.data
    doc.delete()
    Tombstone.objects.create(collection=collection, doc_id=doc_id, rev=rev)
    if collection == "students":
        from . import telegram
        telegram.forget_student(doc_id)
    _refresh_reports(collection, rev, old, None)
    return rev


def _tell_mentor(team_id, name, by_user, student_id=None):
    """Ментору в Telegram: за ним закрепили студента. Уходит после фиксации записи; сбой Telegram запись не отменяет."""
    from . import telegram  # позднее подключение: telegram пользуется этим модулем
    transaction.on_commit(lambda: telegram.notify_team(team_id, f"👤 За вами закреплён студент: {name}. Свяжитесь с ним и назначьте следующий шаг.", skip_user=by_user, student_id=student_id))


def _refresh_reports(collection, rev, old, new):
    """После изменения оплат, расходов, выплат, условий или состава сторон отчёты пересчитываются.

    Выручка направления общая для всех его сторон, поэтому пересчитываются все отчёты; неизменившиеся не перезаписываются.
    """
    if collection not in reports.SOURCES:
        return
    if collection == "students" and old is not None and new is not None \
            and all(old.get(key) == new.get(key) for key in reports.STUDENT_FIELDS):
        return  # заметка, шаг или контакт: цифры отчётов от этого не меняются
    reports.refresh(rev)


def _save(doc):
    try:
        with transaction.atomic():
            doc.save()
    except DataError:
        raise Invalid("Запись содержит данные, которые нельзя сохранить")


@transaction.atomic
def system_write(collection, doc_id, data):
    """Запись от имени сервера (заявки с сайта, импорт копии): без проверки прав пользователя."""
    _validate(collection, doc_id, data)
    rev = _next_rev()
    Tombstone.objects.filter(collection=collection, doc_id=doc_id).delete()
    doc = Doc.objects.filter(collection=collection, doc_id=doc_id).first() or Doc(collection=collection, doc_id=doc_id)
    old_mentor = doc.data.get("mentorId") if doc.pk else None
    old = doc.data if doc.pk else None
    doc.data, doc.rev, doc.updated_by = _clean(collection, data), rev, None
    _save(doc)
    if collection == "students" and doc.data.get("mentorId") != old_mentor:
        _touch_meetings(doc_id, rev)
    _refresh_reports(collection, rev, old, doc.data)
    return rev


@transaction.atomic
def bump_epoch():
    st = lock()
    st.epoch += 1
    st.save(update_fields=["epoch"])
