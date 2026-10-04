"""Отчёты сторон считает сервер: блогер и ментор видят свежие цифры, даже когда никто не открыл CRM.

Деньги делятся по направлениям. Чистая выручка направления за месяц — оплаты его студентов минус расходы;
каждая сторона из условий направления получает от неё свой процент, остальное остаётся владельцу.
Условия действуют с указанного в них месяца, поэтому смена ставок не пересчитывает прошлое.

Расчёт повторяет `settle`, `partyStats` и `partyReport` из web/index.html. В отчёт попадают только цифры
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
SOURCES = ("students", "payouts", "partners", "team", "cohorts", "directions", "expenses")  # от них зависят цифры отчётов
KINDS = ("partner", "mentor")
# Поля студента, от которых зависят отчёты: остальные правки пересчёта не требуют
STUDENT_FIELDS = ("payments", "cohortId", "partnerId", "stage", "history", "name", "createdAt", "deletedAt")


NUMBER_RE = re.compile(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*\Z", re.ASCII)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
MONTH_RE = re.compile(r"\d{4}-\d{2}\Z", re.ASCII)
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


def _day(value):
    return value[:10] if isinstance(value, str) and DATE_RE.match(value) else ""


def _body(data):
    return {k: v for k, v in data.items() if k != "updatedAt"}


def _stamp():
    moment = timezone.now()
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _rows(data, collection):
    """Записи коллекции по порядку идентификаторов: в том же порядке их складывает страница."""
    docs = data.get(collection) or {}
    return [(doc_id, docs[doc_id]) for doc_id in sorted(docs) if isinstance(docs[doc_id], dict)]


def direction_of(student, data):
    """Направление студента — направление его потока. Без потока или направления оплата ни с кем не делится."""
    cohort_id = student.get("cohortId")
    cohort = data["cohorts"].get(cohort_id) if isinstance(cohort_id, str) else None
    linked = cohort.get("directionId") if isinstance(cohort, dict) else None
    return linked if isinstance(linked, str) and isinstance(data["directions"].get(linked), dict) else ""


def terms_for(direction, month):
    """Стороны и ставки направления в этом месяце: [(вид, id, процент)].

    Берутся последние условия, начавшие действовать не позже месяца; для месяцев до первых условий — первые.
    """
    best = first = None
    for term in _list(direction.get("terms")):
        start = term.get("from")
        if not isinstance(start, str) or not MONTH_RE.match(start):
            continue
        if first is None or start < first["from"]:
            first = term
        if start <= month and (best is None or start > best["from"]):
            best = term
    term = best or first
    out, seen = [], set()
    for party in _list(term.get("parties")) if term else []:
        kind, party_id = party.get("kind"), party.get("id")
        if kind not in KINDS or not isinstance(party_id, str) or (kind, party_id) in seen:
            continue
        seen.add((kind, party_id))
        out.append((kind, party_id, min(max(_number(party.get("share")), 0), 100)))
    return out


def settle(data):
    """Расчёт по месяцам и направлениям, от новых месяцев к старым."""
    cells = {}

    def cell(month, direction_id):
        return cells.setdefault((month, direction_id), {"month": month, "directionId": direction_id, "received": 0, "expenses": 0})

    for _, student in _rows(data, "students"):
        if student.get("deletedAt"):
            continue
        direction_id = direction_of(student, data)
        for payment in _list(student.get("payments")):
            day = _day(payment.get("paid"))
            if day:
                cell(day[:7], direction_id)["received"] += _number(payment.get("amount"))
    for _, expense in _rows(data, "expenses"):
        day = _day(expense.get("date"))
        if not day:
            continue
        direction_id = expense.get("directionId")
        if not isinstance(direction_id, str) or not isinstance(data["directions"].get(direction_id), dict):
            direction_id = ""
        cell(day[:7], direction_id)["expenses"] += _number(expense.get("amount"))
    out = []
    for key in sorted(sorted(cells, key=lambda k: k[1]), key=lambda k: k[0], reverse=True):
        c = cells[key]
        c["net"] = c["received"] - c["expenses"]
        parties = terms_for(data["directions"][c["directionId"]], c["month"]) if c["directionId"] else []
        c["parties"] = [{"kind": kind, "id": party_id, "share": share, "accrued": _round(c["net"] * share) / 100}
                        for kind, party_id, share in parties]
        out.append(c)
    return out


def _money(data, kind, party_id, cells):
    """Начисления стороны по месяцам, выплаты и остаток."""
    months = []
    for c in cells:
        for party in c["parties"]:
            if party["kind"] == kind and party["id"] == party_id:
                months.append({"month": c["month"], "directionId": c["directionId"],
                               "direction": _text(data["directions"][c["directionId"]].get("name"), 200),
                               "received": _tidy(c["received"]), "expenses": _tidy(c["expenses"]), "net": _tidy(c["net"]),
                               "share": _tidy(party["share"]), "accrued": _tidy(party["accrued"])})
    key = "partnerId" if kind == "partner" else "mentorId"
    mine = sorted((p for _, p in _rows(data, "payouts") if p.get(key) == party_id), key=lambda p: str(p.get("date") or ""))[::-1]
    accrued = _total(m["accrued"] for m in months)
    paid_out = _total(_number(p.get("amount")) for p in mine)
    return {"months": months, "accrued": _tidy(accrued), "paidOut": _tidy(paid_out), "balance": _tidy(accrued - paid_out),
            "payouts": [{"date": _day(p.get("date")), "amount": _tidy(_number(p.get("amount"))),
                         "comment": _text(p.get("comment"), 300)} for p in mine]}


def build(doc_id, data, cells=None):
    """Отчёт блогера или ментора с таким идентификатором или None. data — см. load()."""
    cells = settle(data) if cells is None else cells
    partner = data["partners"].get(doc_id)
    if isinstance(partner, dict):
        return _partner_report(doc_id, partner, data, cells)
    member = data["team"].get(doc_id)
    if isinstance(member, dict):
        return {"kind": "mentor", "mentorId": doc_id, "name": _text(member.get("name"), 200), **_money(data, "mentor", doc_id, cells)}
    return None


def _partner_report(partner_id, partner, data, cells):
    """К деньгам блогера добавляется воронка по его трафику: сколько людей пришло и до какого этапа дошли."""
    own = [s for _, s in _rows(data, "students") if s.get("partnerId") == partner_id and not s.get("deletedAt")]
    own = sorted(own, key=lambda s: str(s.get("createdAt") or ""))[::-1]
    rows = [{"s": s, "paid": paid_sum(s)} for s in own]
    paid_students = sum(1 for r in rows if r["paid"] > 0)
    tops = [reached(s) for s in own]
    funnel = []
    for index, (key, label) in enumerate(STAGES):
        if key == "lost":
            continue
        count = sum(1 for top in tops if top >= index)
        funnel.append({"key": key, "label": label, "count": count, "pct": _pct(count, len(own))})
    doc = {
        "kind": "partner", "partnerId": partner_id, "name": _text(partner.get("name"), 200), "promo": _text(partner.get("promo"), 100),
        "leads": len(own), "paidStudents": paid_students, "conv": _pct(paid_students, len(own)),
        "employed": sum(1 for s in own if s.get("stage") in EMPLOYED),
        "revenue": _tidy(_total(r["paid"] for r in rows)), "funnel": funnel,
        **_money(data, "partner", partner_id, cells),
        # Этап и дата попадают в отчёт только в ожидаемом виде: произвольный текст из записи студента туда не проходит
        "rows": [{"name": mask(r["s"].get("name")),
                  "stage": r["s"].get("stage") if isinstance(r["s"].get("stage"), str) and r["s"]["stage"] in STAGE_INDEX else "",
                  "date": _day(r["s"].get("createdAt")), "paid": _tidy(r["paid"])}
                 for r in rows],
    }
    if partner.get("demo"):
        doc["demo"] = True
    return doc


def load():
    """Всё, от чего зависят отчёты: {коллекция: {id: данные}}."""
    data = {name: {} for name in SOURCES}
    for doc in Doc.objects.filter(collection__in=SOURCES):
        data[doc.collection][doc.doc_id] = doc.data
    return data


def compute(doc_id, data=None, cells=None):
    """Свежий отчёт стороны или None, если такого партнёра или ментора нет."""
    data = load() if data is None else data
    report = build(doc_id, data, cells)
    return None if report is None else {**report, "updatedAt": _stamp()}


def refresh(rev, only=None):
    """Пересчитывает отчёты после изменения исходных данных. Вызывается внутри транзакции записи.

    Отчёт есть у каждого блогера и ментора, которым выдан доступ, и у тех, кому его открыли раньше.
    only — id сторон, которых могло затронуть изменение; None — все.
    """
    wanted = set(Doc.objects.filter(collection="reports").values_list("doc_id", flat=True))
    wanted |= set(Account.objects.filter(role__in=(Account.Role.PARTNER, Account.Role.MENTOR)).exclude(link_id="")
                  .values_list("link_id", flat=True))
    if only is not None:
        wanted &= {p for p in only if isinstance(p, str)}
    if not wanted:
        return
    data = load()
    cells = settle(data)
    for party_id in sorted(wanted):
        report = Doc.objects.filter(collection="reports", doc_id=party_id).first()
        fresh = compute(party_id, data, cells)
        if fresh is None:
            if report is not None:  # сторону удалили: её отчёт больше никому не показываем
                report.delete()
                Tombstone.objects.create(collection="reports", doc_id=party_id, rev=rev)
            continue
        if report is not None and _body(fresh) == _body(report.data):
            continue
        report = report or Doc(collection="reports", doc_id=party_id)
        report.data, report.rev, report.updated_by = fresh, rev, None
        try:
            with transaction.atomic():
                report.save()
        except DataError:
            pass  # отчёт останется прежним; сама запись, вызвавшая пересчёт, не должна из-за него сорваться
