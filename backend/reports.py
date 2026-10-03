"""Отчёт партнёра считает сервер: партнёр видит свежие цифры, даже когда никто из сотрудников не открыл CRM.

Расчёт повторяет `partnerStats` и `reportDoc` из web/index.html. В отчёт попадают только цифры
и сокращённые имена («Иван П.»), без контактов студентов.
"""
import math
import re

from django.db import DataError, transaction
from django.utils import timezone

from .models import Account, Doc, Tombstone

# Этапы в том же порядке, что STAGES на странице: по нему считается, до какого этапа человек дошёл
STAGES = (
    ("new", "Новая заявка"), ("contacted", "Связались"), ("call", "Созвон"), ("waiting_payment", "Ждём оплату"),
    ("studying", "Обучается"), ("final_project", "Итоговый проект"), ("job_search", "Поиск работы"),
    ("offer", "Оффер"), ("probation_passed", "Прошёл испытательный"), ("lost", "Отказ / выбыл"),
)
STAGE_INDEX = {key: i for i, (key, _) in enumerate(STAGES)}
EMPLOYED = ("offer", "probation_passed")
SOURCES = ("students", "payouts", "partners")  # от этих коллекций зависят цифры отчёта


NUMBER_RE = re.compile(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*\Z", re.ASCII)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
LIMIT = 1e15  # суммы больше этой считаются ошибкой ввода: так расчёт не ломается на бесконечностях


def _number(value):
    """Как `+x || 0` на странице: всё, что не число, считается нулём. Никогда не падает."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str):
        if not NUMBER_RE.match(value):
            return 0
        try:
            value = float(value)
        except (ValueError, OverflowError):
            return 0
    if not isinstance(value, (int, float)):
        return 0
    try:
        return value if abs(value) < LIMIT else 0  # сюда же попадают nan и inf
    except OverflowError:
        return 0


def _total(values):
    """Сложение по порядку, как reduce на странице: встроенный sum в новых версиях Python округляет иначе."""
    total = 0
    for value in values:
        total += value
    return total


def _tidy(value):
    """Целые значения отдаём целыми, как их пишет страница."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _round(value):
    return math.floor(value + 0.5)  # как Math.round


def _pct(part, whole):
    return _round(part * 100 / whole) if whole else 0


def _list(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def paid_sum(student):
    return _total(_number(p.get("amount")) for p in _list(student.get("payments")) if p.get("paid"))


def reached(student):
    top = 0
    for step in [*_list(student.get("history")), {"to": student.get("stage")}]:
        to = step.get("to")
        index = STAGE_INDEX.get(to) if isinstance(to, str) else None
        if to != "lost" and index is not None and index > top:
            top = index
    return top


def mask(name):
    """«Иванов Пётр» → «Пётр И.». Если вместо имени записан контакт (заявка без имени), он не показывается."""
    text = str(name or "") if isinstance(name, (str, int, float)) else ""
    if "@" in text or any(ch.isdigit() for ch in text):
        return "Без имени"
    parts = text.split()
    return f"{parts[1]} {parts[0][0]}." if len(parts) >= 2 else (parts[0] if parts else "")


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


def build(partner_id, partner, students, payouts):
    """Документ отчёта. students и payouts — списки данных документов (dict)."""
    own = [s for s in students if s.get("partnerId") == partner_id and not s.get("deletedAt")]
    own = sorted(own, key=lambda s: str(s.get("createdAt") or ""))[::-1]
    default_share = partner.get("share") if partner.get("share") is not None else 0
    rows = []
    for s in own:
        paid = paid_sum(s)
        percent = s.get("partnerShare") if s.get("partnerShare") is not None else default_share
        rows.append({"s": s, "paid": paid, "share": _round(paid * _number(percent)) / 100})
    mine = sorted((p for p in payouts if p.get("partnerId") == partner_id), key=lambda p: str(p.get("date") or ""))[::-1]
    accrued = _total(r["share"] for r in rows)
    paid_out = _total(_number(p.get("amount")) for p in mine)
    paid_students = sum(1 for r in rows if r["paid"] > 0)
    tops = [reached(s) for s in own]
    funnel = []
    for index, (key, label) in enumerate(STAGES):
        if key == "lost":
            continue
        count = sum(1 for top in tops if top >= index)
        funnel.append({"key": key, "label": label, "count": count, "pct": _pct(count, len(own))})
    doc = {
        "partnerId": partner_id, "name": _text(partner.get("name"), 200), "share": _tidy(_number(default_share)),
        "promo": _text(partner.get("promo"), 100),
        "leads": len(own), "paidStudents": paid_students, "conv": _pct(paid_students, len(own)),
        "employed": sum(1 for s in own if s.get("stage") in EMPLOYED),
        "revenue": _tidy(_total(r["paid"] for r in rows)), "accrued": _tidy(accrued), "paidOut": _tidy(paid_out),
        "balance": _tidy(accrued - paid_out), "funnel": funnel,
        "payouts": [{"date": _day(p.get("date")), "amount": _tidy(_number(p.get("amount"))),
                     "comment": _text(p.get("comment"), 300)} for p in mine],
        # Этап и дата попадают в отчёт только в ожидаемом виде: произвольный текст из записи студента туда не проходит
        "rows": [{"name": mask(r["s"].get("name")),
                  "stage": r["s"].get("stage") if isinstance(r["s"].get("stage"), str) and r["s"]["stage"] in STAGE_INDEX else "",
                  "date": _day(r["s"].get("createdAt")), "paid": _tidy(r["paid"]), "share": _tidy(r["share"])}
                 for r in rows],
    }
    if partner.get("demo"):
        doc["demo"] = True
    return doc


def _day(value):
    return value[:10] if isinstance(value, str) and DATE_RE.match(value) else ""


def _body(data):
    return {k: v for k, v in data.items() if k != "updatedAt"}


def _stamp():
    moment = timezone.now()
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def compute(partner_id):
    """Свежий отчёт партнёра или None, если такого партнёра нет."""
    partner = Doc.objects.filter(collection="partners", doc_id=partner_id).first()
    if partner is None:
        return None
    # Порядок по идентификатору — тот же, в каком записи видит страница: от него зависит порядок строк с одинаковой датой
    students = [d.data for d in Doc.objects.filter(collection="students", data__partnerId=partner_id).order_by("doc_id")]
    payouts = [d.data for d in Doc.objects.filter(collection="payouts", data__partnerId=partner_id).order_by("doc_id")]
    return {**build(partner_id, partner.data, students, payouts), "updatedAt": _stamp()}


def refresh(rev, only=None):
    """Пересчитывает отчёты после изменения исходных данных. Вызывается внутри транзакции записи.

    Отчёт есть у каждого партнёра, которому выдан доступ, и у тех, кому его открыли раньше.
    only — id партнёров, которых могло затронуть изменение; None — все.
    """
    wanted = set(Doc.objects.filter(collection="reports").values_list("doc_id", flat=True))
    wanted |= set(Account.objects.filter(role=Account.Role.PARTNER).exclude(link_id="").values_list("link_id", flat=True))
    if only is not None:
        wanted &= {p for p in only if isinstance(p, str)}
    for partner_id in sorted(wanted):
        report = Doc.objects.filter(collection="reports", doc_id=partner_id).first()
        fresh = compute(partner_id)
        if fresh is None:
            if report is not None:  # партнёра удалили: его отчёт больше никому не показываем
                report.delete()
                Tombstone.objects.create(collection="reports", doc_id=partner_id, rev=rev)
            continue
        if report is not None and _body(fresh) == _body(report.data):
            continue
        report = report or Doc(collection="reports", doc_id=partner_id)
        report.data, report.rev, report.updated_by = fresh, rev, None
        try:
            with transaction.atomic():
                report.save()
        except DataError:
            pass  # отчёт останется прежним; сама запись, вызвавшая пересчёт, не должна из-за него сорваться
