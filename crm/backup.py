"""Импорт резервной копии из веб-версии CRM (страница в Claude).

Файл копии скачивается в веб-версии: Настройки → Резервная копия.
Импорт можно запускать повторно: записи, которые уже перенесены, пропускаются
(сопоставление по external_id), если не указано overwrite.
"""
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from .models import (
    Cohort, JobApplication, Note, Partner, PartnerPayout, Payment, Stage, StageHistory, Student,
)
from .permissions import MENTOR_GROUP

BACKUP_FORMAT = "crm-menti-backup"
# 2: у студента появился следующий шаг (next); 3: программа потока (modules) и прогресс студента (progress)
SUPPORTED_VERSIONS = (1, 2, 3)


class BackupError(ValueError):
    """Файл не похож на резервную копию или повреждён."""


def _dec(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _dt(value):
    """ISO-время из копии. Время без часового пояса считается локальным."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _text(value, limit=None):
    text = "" if value is None else str(value).strip()
    return text[:limit] if limit else text


def _modules(value):
    """Программа потока: только записи с непустыми id и названием."""
    result = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and _text(item.get("id")) and _text(item.get("title")):
            result.append({"id": _text(item["id"], 64), "title": _text(item["title"], 200)})
    return result


def _progress(value):
    """Отметки модулей: оставляем только поставленные, дату приводим к ISO."""
    result = {}
    for key, done in (value if isinstance(value, dict) else {}).items():
        if done:
            day = _date(done)
            result[_text(key, 64)] = day.isoformat() if day else timezone.localdate().isoformat()
    return result


def parse_backup(raw):
    """Принимает bytes, str или dict и возвращает проверенный словарь копии."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BackupError("Файл не в кодировке UTF-8") from exc
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise BackupError("Файл не является JSON") from exc
    if not isinstance(raw, dict) or raw.get("format") != BACKUP_FORMAT:
        raise BackupError("Это не резервная копия CRM")
    if raw.get("version") not in SUPPORTED_VERSIONS:
        raise BackupError(f"Неизвестная версия копии: {raw.get('version')}")
    if not isinstance(raw.get("data"), dict):
        raise BackupError("В копии нет данных")
    return raw


def _rows(data, key, with_demo, keep=()):
    """Записи раздела. Примеры пропускаются, кроме тех, на которые ссылаются настоящие студенты."""
    rows = data.get(key) or []
    return [r for r in rows if isinstance(r, dict) and r.get("id")
            and (with_demo or not r.get("demo") or r["id"] in keep)]


@transaction.atomic
def import_backup(raw, overwrite=False, with_demo=False):
    """Переносит данные копии в базу. Возвращает статистику по каждому разделу.

    overwrite=True заменяет уже перенесённых студентов данными из копии
    (вместе с их платежами, откликами, заметками и историей).
    """
    data = parse_backup(raw)["data"]
    stats = {k: {"created": 0, "updated": 0, "skipped": 0}
             for k in ("cohorts", "team", "partners", "students", "payouts")}
    stats["warnings"] = []

    student_rows = _rows(data, "students", with_demo)
    used = {key: {r.get(key) for r in student_rows} for key in ("cohortId", "mentorId", "partnerId")}

    # --- Потоки ---
    cohorts = {}
    for row in _rows(data, "cohorts", with_demo, used["cohortId"]):
        obj = Cohort.objects.filter(external_id=row["id"]).first()
        modules = _modules(row.get("modules"))
        if obj and modules and (overwrite or not obj.modules):
            # Поток перенесён раньше, из копии без программы: догружаем программу
            obj.modules = modules
            obj.save(update_fields=["modules"])
            stats["cohorts"]["updated"] += 1
        elif obj:
            stats["cohorts"]["skipped"] += 1
        else:
            obj = Cohort.objects.create(
                external_id=row["id"], name=_text(row.get("name"), 100) or "Без названия",
                start_date=_date(row.get("start")), end_date=_date(row.get("end")),
                price=_dec(row.get("price")), is_active=row.get("active") is not False,
                modules=modules,
            )
            stats["cohorts"]["created"] += 1
        cohorts[row["id"]] = obj

    # --- Команда: менторы становятся пользователями без пароля ---
    User = get_user_model()
    mentor_group, _ = Group.objects.get_or_create(name=MENTOR_GROUP)
    mentors, people = {}, {}
    for row in _rows(data, "team", with_demo, used["mentorId"]):
        username = f"mentor_{row['id']}"[:150]
        user = User.objects.filter(username=username).first()
        if user:
            stats["team"]["skipped"] += 1
        else:
            first, _, last = _text(row.get("name")).partition(" ")
            user = User(username=username, first_name=first[:150], last_name=last[:150])
            user.set_unusable_password()
            user.save()
            user.groups.add(mentor_group)
            stats["team"]["created"] += 1
        mentors[row["id"]] = user
        if row.get("userId"):
            people[row["userId"]] = user

    # --- Партнёры ---
    partners = {}
    for row in _rows(data, "partners", with_demo, used["partnerId"]):
        obj = Partner.objects.filter(external_id=row["id"]).first()
        if obj:
            stats["partners"]["skipped"] += 1
        else:
            obj = Partner.objects.create(
                external_id=row["id"], name=_text(row.get("name"), 200) or "Без названия",
                promo_code=_text(row.get("promo"), 50), utm_source=_text(row.get("utm"), 100),
                share_percent=_dec(row.get("share")) or Decimal("0"),
                contacts=_text(row.get("contacts")), is_active=row.get("active") is not False,
            )
            stats["partners"]["created"] += 1
        partners[row["id"]] = obj

    # --- Студенты ---
    for row in student_rows:
        existing = Student.objects.filter(external_id=row["id"]).first()
        if existing and not overwrite:
            stats["students"]["skipped"] += 1
            continue

        stage = row.get("stage")
        if stage not in Stage.values:
            stats["warnings"].append(f"{row.get('name')}: неизвестный этап «{stage}», поставлен «Новая заявка»")
            stage = Stage.NEW
        created_at = _dt(row.get("createdAt")) or timezone.now()
        step = row.get("next") if isinstance(row.get("next"), dict) else {}
        step_date = _date(step.get("date")) if _text(step.get("text")) else None
        partner = partners.get(row.get("partnerId"))
        fields = dict(
            full_name=_text(row.get("name"), 200) or "Без имени",
            phone=_text(row.get("phone"), 50), telegram=_text(row.get("telegram"), 100),
            email=_text(row.get("email"), 254), city=_text(row.get("city"), 100),
            consent_pd=bool(row.get("consent")),
            partner=partner,
            utm_source=_text(row.get("utmSource"), 100), utm_campaign=_text(row.get("utmCampaign"), 200),
            promo_code=_text(row.get("promo"), 50),
            stage=stage, stage_changed_at=_dt(row.get("stageAt")) or created_at,
            lost_reason=_text(row.get("lostReason"), 255),
            cohort=cohorts.get(row.get("cohortId")), mentor=mentors.get(row.get("mentorId")),
            price=_dec(row.get("price")),
            job_company=_text(row.get("jobCompany"), 200), job_position=_text(row.get("jobPosition"), 200),
            job_salary=_dec(row.get("jobSalary")), offer_date=_date(row.get("offerDate")),
            comment=_text(row.get("comment")), created_at=created_at,
            next_step=_text(step.get("text"), 255) if step_date else "", next_step_date=step_date,
            progress=_progress(row.get("progress")),
        )
        if existing:
            for name, value in fields.items():
                setattr(existing, name, value)
            student = existing
            for related in (student.payments, student.applications, student.notes, student.history):
                related.all().delete()
            stats["students"]["updated"] += 1
        else:
            student = Student(external_id=row["id"], **fields)
            stats["students"]["created"] += 1
        student.save()

        # save() сам фиксирует долю партнёра и пишет историю — возвращаем значения из копии
        share = _dec(row.get("partnerShare"))
        Student.objects.filter(pk=student.pk).update(
            partner_share_percent=(share if share is not None else (partner.share_percent if partner else 0))
            if partner else 0,
            stage_changed_at=fields["stage_changed_at"],
        )
        student.history.all().delete()

        history = []
        for h in row.get("history") or []:
            if not isinstance(h, dict) or h.get("to") not in Stage.values:
                continue
            history.append(StageHistory(
                student=student, from_stage=h.get("from") if h.get("from") in Stage.values else "",
                to_stage=h["to"], changed_by=people.get(h.get("by")), changed_at=_dt(h.get("at")) or created_at,
            ))
        if not history:
            history.append(StageHistory(student=student, from_stage="", to_stage=stage, changed_at=created_at))
        StageHistory.objects.bulk_create(history)

        payments = []
        for p in row.get("payments") or []:
            amount, due = _dec(p.get("amount")) if isinstance(p, dict) else None, None
            if isinstance(p, dict):
                due = _date(p.get("due")) or _date(p.get("paid"))
            if amount is None or due is None:
                continue
            paid = _date(p.get("paid"))
            payments.append(Payment(
                student=student, amount=amount, due_date=due, paid_date=paid,
                status=Payment.Status.PAID if paid else Payment.Status.PLANNED,
                comment=_text(p.get("comment"), 255),
            ))
        Payment.objects.bulk_create(payments)

        # bulk_create не вызывает save(), поэтому «Оффер принят» не меняет этап второй раз
        apps = []
        for a in row.get("apps") or []:
            if not isinstance(a, dict) or not _text(a.get("company")):
                continue
            status = a.get("status") if a.get("status") in JobApplication.Status.values else "applied"
            apps.append(JobApplication(
                student=student, company=_text(a.get("company"), 200), position=_text(a.get("position"), 200),
                url=_text(a.get("url"), 200), status=status, salary=_dec(a.get("salary")),
                applied_date=_date(a.get("date")) or timezone.localdate(created_at),
                interview_at=_dt(a.get("interview")), notes=_text(a.get("notes")),
            ))
        JobApplication.objects.bulk_create(apps)

        notes = []
        for n in row.get("notes") or []:
            if not isinstance(n, dict) or not _text(n.get("text")):
                continue
            notes.append(Note(
                student=student, author=people.get(n.get("by")), text=_text(n.get("text")),
                created_at=_dt(n.get("at")) or created_at,
            ))
        Note.objects.bulk_create(notes)

    # --- Выплаты партнёрам ---
    for row in _rows(data, "payouts", with_demo):
        partner = partners.get(row.get("partnerId"))
        amount = _dec(row.get("amount"))
        if not partner or amount is None:
            stats["payouts"]["skipped"] += 1
            continue
        if PartnerPayout.objects.filter(external_id=row["id"]).exists():
            stats["payouts"]["skipped"] += 1
            continue
        PartnerPayout.objects.create(
            external_id=row["id"], partner=partner, amount=amount,
            paid_date=_date(row.get("date")) or timezone.localdate(), comment=_text(row.get("comment"), 255),
        )
        stats["payouts"]["created"] += 1

    return stats


LABELS = {"cohorts": "Потоки", "team": "Менторы", "partners": "Партнёры", "students": "Студенты", "payouts": "Выплаты"}


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
