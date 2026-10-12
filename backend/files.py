"""Файлы в карточке студента: договор и документы к платежам (чек, выписка, скан).

Содержимое лежит в базе зашифрованным (FileBlob), а в записи студента — только сведения о файле:
{id, name, mime, size, at, by} в поле contract или в payments[i].files. Эти сведения пишет только сервер
при загрузке и удалении: правка студента со страницы их не меняет (store._keep_files).

Права: файлы видит и прикрепляет тот, кто ведёт студента (администратор и ментор этого студента). Файлы оплаченного
платежа добавляет и убирает только администратор — оплату подтверждает он. Партнёру файлы недоступны.
"""
import base64
import copy
import hashlib
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.http import content_disposition_header

from . import reports, store
from .models import Account, Doc, FileBlob

MAX_BYTES = 10 * 1024 * 1024
MAX_PER_PAYMENT = 10
MAX_PER_STUDENT = 200 * 1024 * 1024
LOG_MAX = 200  # как на странице
FILE_ID_RE = re.compile(r"[a-f0-9]{24}\Z")
KINDS = ("contract", "payment")
# Тип файла определяется по содержимому, а не по имени: под видом договора не загрузить страницу или скрипт
TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
}


def _fernet():
    # Ключ выводится из SECRET_KEY: при его смене прежние файлы не прочитать (они остаются в ночной копии базы)
    key = hashlib.sha256(b"crm-files:" + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def sniff(content, name=""):
    """MIME по первым байтам файла или None, если тип не поддерживается."""
    head, lower = content[:16], (name or "").lower()
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"PK\x03\x04") and lower.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and lower.endswith(".doc"):
        return "application/msword"
    return None


def clean_name(name, mime):
    """Имя файла для показа и скачивания: без пути и служебных символов, с расширением по настоящему типу."""
    name = re.split(r"[\\/]", str(name or ""))[-1]
    name = re.sub(r"[\x00-\x1f\x7f\"<>|*?:\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", name).strip(" .")[:120] or "файл"
    ext = TYPES[mime]
    if not name.lower().endswith(ext) and not (ext == ".jpg" and name.lower().endswith(".jpeg")):
        name += ext
    return name


def read(blob):
    try:
        return _fernet().decrypt(bytes(blob.data))
    except InvalidToken:
        return None


def store_blob(file_id, student_id, name, mime, content):
    return FileBlob.objects.create(id=file_id, student_id=student_id, name=name, mime=mime, size=len(content),
                                   data=_fernet().encrypt(content))


def refs(data):
    """Идентификаторы файлов, на которые ссылается запись студента."""
    out = set()
    contract = data.get("contract") if isinstance(data, dict) else None
    if isinstance(contract, dict) and isinstance(contract.get("id"), str):
        out.add(contract["id"])
    pays = data.get("payments") if isinstance(data, dict) else None
    for p in pays if isinstance(pays, list) else []:
        files = p.get("files") if isinstance(p, dict) else None
        for f in files if isinstance(files, list) else []:
            if isinstance(f, dict) and isinstance(f.get("id"), str):
                out.add(f["id"])
    return out


def keep(old, new):
    """Сведения о файлах в новой записи студента берутся из прежней: их меняют только загрузка и удаление файла."""
    contract = old.get("contract") if isinstance(old, dict) else None
    if isinstance(contract, dict):
        new["contract"] = contract
    else:
        new.pop("contract", None)
    before = {}
    old_pays = old.get("payments") if isinstance(old, dict) else None
    for p in old_pays if isinstance(old_pays, list) else []:
        if isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("files"), list) and p["files"]:
            before[p["id"]] = p["files"]
    pays = new.get("payments")
    for p in pays if isinstance(pays, list) else []:
        if not isinstance(p, dict):
            continue
        files = before.get(p.get("id")) if isinstance(p.get("id"), str) else None
        if files:
            p["files"] = files
        else:
            p.pop("files", None)


def prune(student_id, data):
    """Удаляет файлы, на которые запись больше не ссылается (например, удалили неоплаченный платёж)."""
    FileBlob.objects.filter(student_id=student_id).exclude(id__in=refs(data)).delete()


def _staff(access):
    if access.role not in (Account.Role.ADMIN, Account.Role.MENTOR):
        raise store.Denied


def _student(access, student_id):
    doc = Doc.objects.select_for_update().filter(collection="students", doc_id=student_id).first() \
        if isinstance(student_id, str) and store.ID_RE.fullmatch(student_id) else None
    if doc is None or not access.can_read("students", doc.data):
        raise store.Denied  # чужой и несуществующий студент отвечают одинаково
    return doc


def _paid(p):
    # Так же, как проверка денег в store (Access._check_money): любая отметка оплаты считается оплатой
    return bool(p.get("paid"))


def _log(data, access, text):
    """Запись в журнал изменений по деньгам, как делает страница (logItem)."""
    log = data.get("log") if isinstance(data.get("log"), list) else []
    data["log"] = (log + [{"id": secrets.token_hex(10), "at": timezone.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                           "by": store.user_key(access.user), "text": text}])[-LOG_MAX:]


def _pay_label(p):
    amount = reports._number(p.get("amount"))
    due = p.get("due") if isinstance(p.get("due"), str) else ""
    return f"{amount:,.0f} ₽".replace(",", " ") + (f" со сроком {due[8:10]}.{due[5:7]}.{due[:4]}" if reports._day(due) else "") if amount else "платёж от дохода"


def _payment(data, payment_id):
    pays = data.get("payments")
    for p in pays if isinstance(pays, list) else []:
        if isinstance(p, dict) and p.get("id") == payment_id:
            return p
    return None


@transaction.atomic
def upload(access, student_id, kind, payment_id, upfile):
    """Прикрепляет файл. Возвращает (ревизия, сведения о файле)."""
    _staff(access)
    if kind not in KINDS:
        raise store.Invalid("Неизвестно, куда прикрепить файл")
    if upfile is None:
        raise store.Invalid("Выберите файл")
    if upfile.size > MAX_BYTES:
        raise store.Invalid("Файл больше 10 МБ")
    content = upfile.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise store.Invalid("Файл больше 10 МБ")
    mime = sniff(content, upfile.name)
    if mime is None:
        raise store.Invalid("Подходят PDF, JPG, PNG, WebP и документы Word")
    store.lock()  # первой берётся блокировка состояния: тот же порядок, что и при записи документов
    doc = _student(access, student_id)
    if doc.data.get("deletedAt"):
        raise store.Invalid("Студент в корзине")
    used = sum(FileBlob.objects.filter(student_id=doc.doc_id).values_list("size", flat=True))
    if used + len(content) > MAX_PER_STUDENT:
        raise store.Invalid("У студента уже слишком много файлов: уберите старые")
    data = copy.deepcopy(doc.data)
    meta = {"id": secrets.token_hex(12), "name": clean_name(upfile.name, mime), "mime": mime, "size": len(content),
            "at": timezone.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"), "by": store.user_key(access.user)}
    if kind == "contract":
        data["contract"] = meta  # прежний договор заменяется, его файл удалит prune
    else:
        p = _payment(data, payment_id) if isinstance(payment_id, str) else None
        if p is None:
            raise store.Invalid("Платёж не найден: возможно, его удалили")
        if _paid(p) and not access.is_admin:
            raise store.Denied  # к оплаченному платежу файлы добавляет администратор
        files = [f for f in p.get("files") or [] if isinstance(f, dict)] if isinstance(p.get("files"), list) else []
        if len(files) >= MAX_PER_PAYMENT:
            raise store.Invalid(f"К платежу можно прикрепить не больше {MAX_PER_PAYMENT} файлов")
        p["files"] = files + [meta]
        _log(data, access, f"К платежу {_pay_label(p)} прикреплён файл «{meta['name']}»")
    store_blob(meta["id"], doc.doc_id, meta["name"], mime, content)
    rev = store.system_write("students", doc.doc_id, data)
    prune(doc.doc_id, data)
    return rev, meta


@transaction.atomic
def remove(access, file_id):
    """Убирает файл из карточки и удаляет его содержимое. Возвращает ревизию."""
    _staff(access)
    st = store.lock()
    blob = FileBlob.objects.filter(pk=file_id).first() if FILE_ID_RE.fullmatch(file_id or "") else None
    if blob is None:
        raise store.Denied
    doc = _student(access, blob.student_id)
    data = copy.deepcopy(doc.data)
    contract = data.get("contract")
    if isinstance(contract, dict) and contract.get("id") == file_id:
        data.pop("contract")
    else:
        pays = data.get("payments")
        for p in pays if isinstance(pays, list) else []:
            if isinstance(p, dict) and isinstance(p.get("files"), list) and any(isinstance(f, dict) and f.get("id") == file_id for f in p["files"]):
                if _paid(p) and not access.is_admin:
                    raise store.Denied
                _log(data, access, f"У платежа {_pay_label(p)} убран файл «{blob.name}»")
                p["files"] = [f for f in p["files"] if not (isinstance(f, dict) and f.get("id") == file_id)]
                if not p["files"]:
                    p.pop("files")
                break
        else:
            blob.delete()  # на файл никто не ссылается: просто убираем
            return st.rev
    rev = store.system_write("students", doc.doc_id, data)
    prune(doc.doc_id, data)
    return rev


@transaction.atomic
def serve(access, file_id):
    _staff(access)
    blob = FileBlob.objects.filter(pk=file_id).first() if FILE_ID_RE.fullmatch(file_id or "") else None
    doc = Doc.objects.filter(collection="students", doc_id=blob.student_id).first() if blob else None
    if doc is None or not access.can_read("students", doc.data) or file_id not in refs(doc.data):
        raise store.Denied  # чужой файл и несуществующий отвечают одинаково
    content = read(blob)
    if content is None:
        raise store.NotFound
    mime = blob.mime if blob.mime in TYPES else "application/octet-stream"
    response = HttpResponse(content, content_type=mime)
    # Файл мог прислать посторонний человек: браузер не выполняет его и не угадывает тип
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    # Картинка открывается во вкладке, остальное скачивается: встроенный просмотр PDF под такой защитой не работает
    response["Content-Disposition"] = content_disposition_header(not mime.startswith("image/"), blob.name)
    response["Cache-Control"] = "private, no-store"
    return response


def forget_student(student_id):
    FileBlob.objects.filter(student_id=student_id).delete()
