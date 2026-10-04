"""Загрузка резервной копии, скачанной на странице CRM (в том числе из версии в Claude).

Файл копии — те же документы, что хранит сервер, поэтому импорт просто раскладывает их по коллекциям.
Повторный импорт не создаёт дублей: существующие записи пропускаются, если не указано overwrite.
"""
import json

from django.db import transaction

from . import store
from .models import Doc

BACKUP_FORMAT = "crm-menti-backup"
SUPPORTED_VERSIONS = (1, 2, 3, 4, 5)  # с версии 5 в копии есть настройки распределения дохода
SECTIONS = ("cohorts", "team", "partners", "students", "payouts", "meetings")
LABELS = {"cohorts": "Потоки", "team": "Команда", "partners": "Партнёры", "students": "Студенты",
          "payouts": "Выплаты", "meetings": "Встречи"}


class BackupError(ValueError):
    """Файл не похож на резервную копию или повреждён."""


def parse_backup(raw):
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
def import_backup(raw, overwrite=False, with_demo=False):
    data = parse_backup(raw)["data"]
    stats = {k: {"created": 0, "updated": 0, "skipped": 0} for k in SECTIONS}
    students = [r for r in _rows(data, "students") if with_demo or not r.get("demo")]
    # Примеры пропускаются, кроме потоков, менторов и партнёров, на которые ссылаются настоящие студенты
    def refs(key):
        return {r.get(key) for r in students if isinstance(r.get(key), str)}
    used = {"cohorts": refs("cohortId"), "team": refs("mentorId"), "partners": refs("partnerId")}
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
    _import_split(data.get("settings"), overwrite)
    return stats


def _import_split(settings, overwrite):
    """Правило распределения дохода из копии. Существующее правило заменяется только при overwrite."""
    split = settings.get("split") if isinstance(settings, dict) else None
    if not isinstance(split, dict):
        return
    share, base = split.get("mentorShare"), split.get("mentorBase")
    if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 <= share <= 100:
        return
    current = Doc.objects.filter(collection="config", doc_id="main").first()
    config = dict(current.data) if current else {}
    if "split" in config and not overwrite:
        return
    config["split"] = {"mentorShare": share, "mentorBase": "afterPartner" if base == "afterPartner" else "full"}
    store.system_write("config", "main", config)


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
