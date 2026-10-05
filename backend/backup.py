"""Загрузка резервной копии, скачанной на странице CRM (в том числе из версии в Claude).

Файл копии — те же документы, что хранит сервер, поэтому импорт просто раскладывает их по коллекциям.
Повторный импорт не создаёт дублей: существующие записи пропускаются, если не указано overwrite.
"""
import base64
import hashlib
import json
import secrets
import zlib

from cryptography.fernet import Fernet, InvalidToken
from django.db import transaction
from django.utils import timezone

from . import store
from .models import Doc

BACKUP_FORMAT = "crm-menti-backup"
SUPPORTED_VERSIONS = (1, 2, 3, 4, 5, 6)  # с версии 6 в копии есть направления и расходы
SECTIONS = ("directions", "cohorts", "team", "partners", "students", "payouts", "meetings", "expenses")
LABELS = {"cohorts": "Потоки", "team": "Команда", "partners": "Партнёры", "students": "Студенты",
          "payouts": "Выплаты", "meetings": "Встречи", "directions": "Направления", "expenses": "Расходы"}


MAGIC = b"CRMENC1\n"       # начало зашифрованной копии
KDF_ROUNDS = 600_000
MIN_PASSPHRASE = 12
MAX_PLAIN_BYTES = 50 * 1024 * 1024   # распакованная копия: с запасом для любой базы CRM, но не больше, чем выдержит сервер


class BackupError(ValueError):
    """Файл не похож на резервную копию или повреждён."""


def passphrase_ok(passphrase):
    return isinstance(passphrase, str) and len(passphrase) >= MIN_PASSPHRASE


def _key(passphrase, salt):
    return base64.urlsafe_b64encode(hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, KDF_ROUNDS, 32))


def encrypt(raw, passphrase):
    """Сжимает и шифрует копию паролем. Без пароля файл не прочитать: его можно хранить вне сервера."""
    salt = secrets.token_bytes(16)
    return MAGIC + salt + Fernet(_key(passphrase, salt)).encrypt(zlib.compress(raw, 9))


def decrypt(blob, passphrase):
    if not passphrase:
        raise BackupError("Копия зашифрована: введите пароль копии")
    salt, token = blob[len(MAGIC):len(MAGIC) + 16], blob[len(MAGIC) + 16:]
    try:
        packed = Fernet(_key(passphrase, salt)).decrypt(token)
        unpacker = zlib.decompressobj()
        raw = unpacker.decompress(packed, MAX_PLAIN_BYTES)
    except (InvalidToken, ValueError, zlib.error):
        raise BackupError("Пароль копии не подошёл или файл повреждён")
    if unpacker.unconsumed_tail:
        raise BackupError("Копия слишком большая")
    return raw


def dump():
    """Вся база в том же виде, в каком её скачивает страница: с корзиной, без отчётов (их считает сервер)."""
    data = {section: [] for section in SECTIONS}
    for doc in Doc.objects.filter(collection__in=SECTIONS).order_by("collection", "doc_id"):
        data[doc.collection].append({**doc.data, "id": doc.doc_id})
    moment = timezone.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({"format": BACKUP_FORMAT, "version": SUPPORTED_VERSIONS[-1], "exportedAt": moment, "data": data},
                      ensure_ascii=False).encode()


def parse_backup(raw, passphrase=None):
    if isinstance(raw, bytes) and raw.startswith(MAGIC):
        raw = decrypt(raw, passphrase)
    if isinstance(raw, (bytes, str)):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            raise BackupError("Файл не читается как JSON")
    if not isinstance(raw, dict) or raw.get("format") != BACKUP_FORMAT:
        raise BackupError("Это не резервная копия CRM")
    if raw.get("version") not in SUPPORTED_VERSIONS:
        raise BackupError(f"Неизвестная версия копии: {raw.get('version')}")
    if not isinstance(raw.get("data"), dict):
        raise BackupError("В копии нет данных")
    return raw


def _rows(data, key):
    rows = data.get(key) or []
    if not isinstance(rows, list):
        raise BackupError(f"Раздел «{key}» повреждён")
    return [r for r in rows if isinstance(r, dict) and store.ID_RE.fullmatch(str(r.get("id") or ""))]


def _ref(value, ids):
    """Ссылка на загруженную запись. В повреждённой копии на месте идентификатора может быть что угодно."""
    return isinstance(value, str) and value in ids


@transaction.atomic
def import_backup(raw, overwrite=False, with_demo=False, passphrase=None):
    data = parse_backup(raw, passphrase)["data"]
    stats = {k: {"created": 0, "updated": 0, "skipped": 0} for k in SECTIONS}
    students = [r for r in _rows(data, "students") if with_demo or not r.get("demo")]
    # Примеры пропускаются, кроме потоков, менторов и партнёров, на которые ссылаются настоящие студенты
    def refs(key):
        return {r.get(key) for r in students if isinstance(r.get(key), str)}
    used = {"cohorts": refs("cohortId"), "team": refs("mentorId"), "partners": refs("partnerId")}
    # Направление нужно, если на него ссылается поток, который будет загружен
    used["directions"] = {r.get("directionId") for r in _rows(data, "cohorts")
                          if isinstance(r.get("directionId"), str) and (with_demo or not r.get("demo") or r["id"] in used["cohorts"])}
    kept = {"students": {r["id"] for r in students}}
    existing = {(d.collection, d.doc_id) for d in Doc.objects.only("collection", "doc_id")}
    for section in SECTIONS:
        rows = students if section == "students" else _rows(data, section)
        for row in rows:
            row = dict(row)
            doc_id = str(row.pop("id"))
            if section != "students" and row.get("demo") and not with_demo and doc_id not in used.get(section, ()):
                continue
            if section == "payouts" and not _ref(row.get("partnerId"), kept.get("partners", ())) \
                    and not _ref(row.get("mentorId"), kept.get("team", ())):
                stats[section]["skipped"] += 1
                continue
            if section == "meetings" and row.get("studentId") and row["studentId"] not in kept["students"]:
                stats[section]["skipped"] += 1
                continue
            kept.setdefault(section, set()).add(doc_id)
            if section == "team" and row.get("userId"):
                # Ключ человека из версии в Claude: по нему подписываются его старые заметки
                row["legacyUserId"] = row.pop("userId")
            if (section, doc_id) in existing:
                if not overwrite:
                    stats[section]["skipped"] += 1
                    continue
                stats[section]["updated"] += 1
            else:
                stats[section]["created"] += 1
            store.system_write(section, doc_id, row)
    return stats


def summary_lines(stats):
    lines = []
    for key, label in LABELS.items():
        s = stats[key]
        parts = [f"добавлено {s['created']}"]
        if s["updated"]:
            parts.append(f"обновлено {s['updated']}")
        if s["skipped"]:
            parts.append(f"пропущено {s['skipped']}")
        lines.append(f"{label}: {', '.join(parts)}")
    return lines
