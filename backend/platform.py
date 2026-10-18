"""Связка с платформой курсов (INTEGRATION.md): CRM сообщает платформе, кому открыть курсы, и забирает сводку обучения.

Обращается только CRM: подписанные запросы по HTTPS к `PLATFORM_URL`. На платформу уходят только номер карточки,
Telegram-номер из чата, привязанного личной ссылкой, сокращённое имя, поток, курсы и ментор. Телефоны, почта и деньги
не уходят. Обращения к платформе не делаются внутри транзакции записи: сбой платформы не ломает работу CRM.
"""
import datetime
import hashlib
import hmac
import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from . import meetings, reports, store, telegram
from .models import Account, Doc, Tombstone, TgChat

log = logging.getLogger(__name__)

PREFIX = "/api/integration/crm/v1"
TIMEOUT = 15
SENT_KEY = "platform:sent:"          # отпечаток последнего отправленного состояния студента
FORGOT_KEY = "platform:forgot:"      # удалённым насовсем уже отправлено «забыть»
SINCE_KEY = "platform:progress-since"
COURSES_KEY = "platform:courses"
KEEP = 30 * 86400
STUDY_ACCESS = {"study": "active", "job": "alumni", "closed": "paused"}


def enabled():
    url = getattr(settings, "PLATFORM_URL", "") or ""
    key = getattr(settings, "MENTI_INTEGRATION_KEY", "") or ""
    return url.startswith("https://") and len(key) >= 32


class PlatformError(Exception):
    def __init__(self, status, code=""):
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


def _call(method, path, body=None):
    raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    full_path = PREFIX + path
    stamp = str(int(time.time()))
    message = b"\n".join([stamp.encode(), method.encode(), full_path.encode(), raw])
    signature = hmac.new(settings.MENTI_INTEGRATION_KEY.encode(), message, hashlib.sha256).hexdigest()
    request = Request(
        settings.PLATFORM_URL.rstrip("/") + full_path,
        data=raw if method != "GET" else None,
        method=method,
        headers={"Content-Type": "application/json", "X-Menti-Time": stamp, "X-Menti-Signature": signature},
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"null")
    except HTTPError as exc:
        try:
            code = json.loads(exc.read() or b"{}").get("code", "")
        except (ValueError, AttributeError):
            code = ""
        raise PlatformError(exc.code, code) from None
    except (URLError, OSError, ValueError) as exc:
        raise PlatformError(0, exc.__class__.__name__) from None


# ---------- Что отправить платформе ----------

def _student_chat(student_id):
    """Telegram-номер только из чата, привязанного личной ссылкой (TgChat.student_id); введённый номер не годится."""
    chat = TgChat.objects.filter(student_id=student_id, user__isnull=True).order_by("-updated_at").first()
    return chat.chat_id if chat else None


def _mentor_chat(mentor_id):
    account = Account.objects.filter(role=Account.Role.MENTOR, link_id=mentor_id).select_related("user").first()
    if account is None:
        return None
    chat = TgChat.objects.filter(user=account.user).order_by("-updated_at").first()
    return chat.chat_id if chat else None


def _next_meeting(cohort_id, cohorts, meeting_docs, now):
    """Ближайшее занятие потока: дата и время, название, ссылка (у встречи или постоянная ссылка потока)."""
    best = None
    for meeting in meeting_docs:
        if cohort_id not in meetings.guests(meeting).cohorts or (meeting.get("status") or "planned") != "planned":
            continue
        day = reports._day(meeting.get("date"))
        clock = meeting.get("time")
        match = meetings.CLOCK_RE.match(clock) if isinstance(clock, str) else None
        if not day or not match:
            continue
        try:
            starts = timezone.make_aware(datetime.datetime.fromisoformat(f"{day}T{match[1]}:{match[2]}"))
        except ValueError:
            continue
        if starts < now - datetime.timedelta(hours=2):
            continue
        if best is None or starts < best[0]:
            best = (starts, meeting)
    if best is None:
        return None
    link = meetings.link_of(best[1], cohorts)
    return {"title": reports._text(best[1].get("title"), 180), "at": best[0].isoformat(), "link": link if link.startswith("https://") else ""}


def desired_state(student_id, student, cohorts, meeting_docs, stages, now):
    """Желаемое состояние студента для платформы или None, если платформе о нём сообщать нечего."""
    if not isinstance(student, dict):
        return None
    group = "closed" if student.get("deletedAt") else stages.group.get(student.get("stage"))
    access = STUDY_ACCESS.get(group)
    if access is None:
        return None  # заявка ещё на этапе продаж: курсов у неё нет
    telegram_id = _student_chat(student_id)
    if telegram_id is None:
        return None  # не привязан к боту личной ссылкой — платформа не узнает его при входе
    cohort_id = student.get("cohortId") if isinstance(student.get("cohortId"), str) else None
    cohort = cohorts.get(cohort_id) if cohort_id else None
    courses = [slug for slug in (cohort or {}).get("courses") or [] if isinstance(slug, str)][:20]
    state = {
        "telegramId": telegram_id,
        "name": reports.mask(student.get("name")) or "Студент",
        "access": access,
        "courses": courses,
        "crmUrl": telegram.crm_link(student_id) if settings.PUBLIC_URL.startswith("https://") else "",
    }
    if cohort:
        state["cohort"] = {
            "id": cohort_id,
            "title": reports._text(cohort.get("name"), 180),
            "starts": reports._day(cohort.get("start")) or None,
            "ends": reports._day(cohort.get("end")) or None,
            "nextMeeting": _next_meeting(cohort_id, cohorts, meeting_docs, now),
        }
    mentor_id = student.get("mentorId") if isinstance(student.get("mentorId"), str) else None
    mentor_chat = _mentor_chat(mentor_id) if mentor_id else None
    if mentor_chat:
        team = Doc.objects.filter(collection="team", doc_id=mentor_id).first()
        state["mentor"] = {"crmId": mentor_id, "telegramId": mentor_chat, "name": reports.mask((team.data if team else {}).get("name")) or "Ментор"}
    return state


def _fingerprint(state):
    # Время ближайшего занятия меняется само собой — оно входит в отпечаток, поэтому новое занятие тоже уйдёт
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _set_sync(student_id, value):
    """Состояние связки в карточке (platformSync) пишет только сервер; запись — только если оно изменилось."""
    with transaction.atomic():
        store.lock()
        doc = Doc.objects.select_for_update().filter(collection="students", doc_id=student_id).first()
        if doc is None or doc.data.get("platformSync") == value:
            return
        data = dict(doc.data)
        data["platformSync"] = value
        store.system_write("students", student_id, data)


def push_states(now=None, full=False):
    """Отправляет платформе изменившиеся состояния студентов. full=True — ночная сверка: всех, даже без изменений."""
    if not enabled():
        return 0
    now = timezone.localtime(now) if now else timezone.localtime()
    stages = reports.current_stages()
    cohorts = {d.doc_id: d.data for d in Doc.objects.filter(collection="cohorts") if isinstance(d.data, dict)}
    meeting_docs = [d.data for d in Doc.objects.filter(collection="meetings") if isinstance(d.data, dict)]
    sent = 0
    for doc in Doc.objects.filter(collection="students").order_by("doc_id"):
        student = doc.data if isinstance(doc.data, dict) else {}
        state = desired_state(doc.doc_id, student, cohorts, meeting_docs, stages, now)
        if state is None:
            if stages.group.get(student.get("stage")) in STUDY_ACCESS and not student.get("deletedAt"):
                _set_sync(doc.doc_id, {"state": "not_linked"})  # учится, но не привязан к боту: подсказка в карточке
            continue
        mark = _fingerprint(state)
        if not full and cache.get(SENT_KEY + doc.doc_id) == mark:
            continue
        try:
            answer = _call("PUT", f"/students/{doc.doc_id}/", state)
        except PlatformError as exc:
            if exc.status == 409:
                _set_sync(doc.doc_id, {"state": "conflict"})
            elif exc.status == 404:
                _set_sync(doc.doc_id, {"state": "course_missing"})
            else:
                log.warning("Платформа: состояние студента не отправлено (%s)", exc)
                return sent  # платформа недоступна: остальных отправим на следующем проходе
            cache.set(SENT_KEY + doc.doc_id, mark, KEEP)
            continue
        cache.set(SENT_KEY + doc.doc_id, mark, KEEP)
        _set_sync(doc.doc_id, {"state": "linked", "platformUserId": answer.get("platformUserId") if isinstance(answer, dict) else None})
        sent += 1
    return sent


def forget_deleted():
    """Удалённым из корзины насовсем — «забыть» на платформе (отвязать и обезличить). Повтор безопасен."""
    if not enabled():
        return
    for doc_id in Tombstone.objects.filter(collection="students").values_list("doc_id", flat=True).distinct():
        if cache.get(FORGOT_KEY + doc_id) or Doc.objects.filter(collection="students", doc_id=doc_id).exists():
            continue
        try:
            _call("POST", f"/students/{doc_id}/forget/")
        except PlatformError as exc:
            log.warning("Платформа: «забыть» не отправлено (%s)", exc)
            return
        cache.set(FORGOT_KEY + doc_id, 1, 365 * 86400)
        cache.delete(SENT_KEY + doc_id)


# ---------- Сводка обучения с платформы ----------

def _same(a, b):
    """Сводки равны, если не считать времени её составления: иначе запись менялась бы каждые 10 минут."""
    strip = lambda value: {k: v for k, v in value.items() if k != "updatedAt"} if isinstance(value, dict) else value  # noqa: E731
    return strip(a) == strip(b)


def pull_progress():
    """Забирает с платформы сводки, изменившиеся с прошлого раза, и пишет их в поле learning карточки."""
    if not enabled():
        return 0
    written = 0
    since = cache.get(SINCE_KEY) or ""
    for _ in range(20):  # не больше 20 страниц за проход
        try:
            page = _call("GET", f"/progress/?since={_quote(since)}&limit=200")
        except PlatformError as exc:
            log.warning("Платформа: сводка не получена (%s)", exc)
            return written
        items = page.get("items") if isinstance(page, dict) else None
        for item in items or []:
            crm_id, learning = item.get("crmId"), item.get("learning")
            if not isinstance(crm_id, str) or not isinstance(learning, dict):
                continue
            with transaction.atomic():
                store.lock()
                doc = Doc.objects.select_for_update().filter(collection="students", doc_id=crm_id).first()
                if doc is None or _same(doc.data.get("learning"), learning):
                    continue
                data = dict(doc.data)
                data["learning"] = learning
                store.system_write("students", crm_id, data)
                written += 1
        if page.get("next"):
            since = page["next"]
            cache.set(SINCE_KEY, since, KEEP)
        if not items or len(items) < 200:
            break
    return written


def _quote(value):
    from urllib.parse import quote
    return quote(str(value), safe="")


def courses():
    """Курсы платформы для выбора у потока: [{slug, title, lessonsTotal}]. Кэшируется на 5 минут."""
    if not enabled():
        return None
    cached = cache.get(COURSES_KEY)
    if cached is not None:
        return cached
    try:
        items = _call("GET", "/courses/")
    except PlatformError as exc:
        log.warning("Платформа: список курсов не получен (%s)", exc)
        return None
    items = [i for i in items or [] if isinstance(i, dict) and isinstance(i.get("slug"), str)]
    cache.set(COURSES_KEY, items, 300)
    return items


# ---------- Для утренней сводки и «Сегодня» ----------

STUCK_DAYS = 7


def stuck_in_learning(student, today):
    """Учится, но не занимается на платформе неделю (или ещё ни разу не входил после открытия курса)."""
    learning = student.get("learning") if isinstance(student, dict) else None
    if not isinstance(learning, dict) or not learning.get("courses"):
        return False
    last = reports._day(learning.get("lastActivityAt"))
    if not last:
        return True
    try:
        return (today - datetime.date.fromisoformat(last)).days >= STUCK_DAYS
    except ValueError:
        return False


def open_reviews(student):
    learning = student.get("learning") if isinstance(student, dict) else None
    courses_ = learning.get("courses") if isinstance(learning, dict) else None
    return sum(reports._number(c.get("reviewsOpen")) for c in courses_ or [] if isinstance(c, dict))
