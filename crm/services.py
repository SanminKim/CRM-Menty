"""Расчёт метрик для дашборда и отчёта партнёра."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Sum
from django.utils import timezone

from .models import (
    EMPLOYED_STAGES, STAGE_ORDER, JobApplication, Payment, Stage, StageHistory, Student,
)


def pct(part, whole):
    return round(part * 100 / whole) if whole else 0


def max_reached_index(students):
    """Для каждого студента — самый дальний этап, до которого он дошёл (без учёта «Отказ»)."""
    ids = [s.pk for s in students]
    reached = {sid: 0 for sid in ids}
    lost_idx = STAGE_ORDER.index(Stage.LOST)
    rows = StageHistory.objects.filter(student_id__in=ids).values_list("student_id", "to_stage")
    for sid, stage in rows:
        idx = STAGE_ORDER.index(stage)
        if idx != lost_idx and idx > reached[sid]:
            reached[sid] = idx
    for s in students:
        idx = STAGE_ORDER.index(s.stage)
        if idx != lost_idx and idx > reached[s.pk]:
            reached[s.pk] = idx
    return reached


def funnel(students):
    """Сколько человек дошли до каждого этапа — классическая воронка."""
    students = list(students)
    reached = max_reached_index(students)
    total = len(students)
    rows = []
    for idx, stage in enumerate(STAGE_ORDER):
        if stage == Stage.LOST:
            continue
        count = sum(1 for v in reached.values() if v >= idx)
        rows.append({
            "stage": stage,
            "label": Stage(stage).label,
            "count": count,
            "pct": pct(count, total),
        })
    return rows


def dashboard_metrics(students_qs):
    today = timezone.localdate()
    students = list(students_qs)
    ids = [s.pk for s in students]
    total = len(students)

    by_stage = {row["stage"]: row["n"] for row in
                students_qs.values("stage").annotate(n=Count("id"))}
    stages = [{"stage": s.value, "label": s.label, "count": by_stage.get(s.value, 0)} for s in Stage]

    payments = Payment.objects.filter(student_id__in=ids)
    paid_students = payments.filter(status=Payment.Status.PAID).values("student").distinct().count()

    reached = max_reached_index(students)
    job_idx = STAGE_ORDER.index(Stage.JOB_SEARCH)
    graduates = sum(1 for v in reached.values() if v >= job_idx)
    employed = [s for s in students if s.stage in EMPLOYED_STAGES]
    salaries = [s.job_salary for s in employed if s.job_salary]
    days_to_offer = [
        (s.offer_date - timezone.localdate(s.created_at)).days
        for s in employed if s.offer_date
    ]

    month_start = today.replace(day=1)
    revenue_month = payments.filter(
        status=Payment.Status.PAID, paid_date__gte=month_start
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0")
    revenue_total = payments.filter(status=Payment.Status.PAID).aggregate(
        s=Sum("amount"))["s"] or Decimal("0")
    overdue = payments.filter(status=Payment.Status.PLANNED, due_date__lt=today).select_related("student")
    upcoming = payments.filter(
        status=Payment.Status.PLANNED, due_date__gte=today, due_date__lte=today + timedelta(days=14)
    ).select_related("student")

    stuck = [s for s in students if s.is_stuck]
    stuck.sort(key=lambda s: -s.days_in_stage)

    interviews = JobApplication.objects.filter(
        student_id__in=ids,
        interview_at__date__gte=today,
        interview_at__date__lte=today + timedelta(days=7),
    ).select_related("student").order_by("interview_at")

    return {
        "total": total,
        "stages": stages,
        "funnel": funnel(students),
        "paid_students": paid_students,
        "conv_paid": pct(paid_students, total),
        "studying": by_stage.get(Stage.STUDYING, 0) + by_stage.get(Stage.FINAL_PROJECT, 0),
        "job_search": by_stage.get(Stage.JOB_SEARCH, 0),
        "graduates": graduates,
        "employed": len(employed),
        "employment_rate": pct(len(employed), graduates),
        "avg_salary": (sum(salaries) / len(salaries)) if salaries else None,
        "avg_days_to_offer": round(sum(days_to_offer) / len(days_to_offer)) if days_to_offer else None,
        "revenue_month": revenue_month,
        "revenue_total": revenue_total,
        "overdue": overdue,
        "overdue_sum": overdue.aggregate(s=Sum("amount"))["s"] or Decimal("0"),
        "upcoming": upcoming,
        "upcoming_sum": upcoming.aggregate(s=Sum("amount"))["s"] or Decimal("0"),
        "stuck": stuck[:15],
        "stuck_days": settings.CRM_STUCK_DAYS,
        "interviews": interviews,
    }


def partner_report(partner, date_from=None, date_to=None):
    students = Student.objects.filter(partner=partner)
    if date_from:
        students = students.filter(created_at__date__gte=date_from)
    if date_to:
        students = students.filter(created_at__date__lte=date_to)
    students = list(students.order_by("-created_at"))

    payments = Payment.objects.filter(student__partner=partner, status=Payment.Status.PAID)
    if date_from:
        payments = payments.filter(paid_date__gte=date_from)
    if date_to:
        payments = payments.filter(paid_date__lte=date_to)
    revenue = payments.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    paid_ids = set(
        Payment.objects.filter(student__in=students, status=Payment.Status.PAID)
        .values_list("student_id", flat=True)
    )
    rows = []
    for s in students:
        paid = s.total_paid
        rows.append({
            "student": s,
            "paid": paid,
            "share": (paid * s.partner_share_percent / Decimal("100")).quantize(Decimal("0.01")),
        })

    employed = sum(1 for s in students if s.stage in EMPLOYED_STAGES)
    return {
        "leads": len(students),
        "paid_students": len(paid_ids),
        "conv": pct(len(paid_ids), len(students)),
        "employed": employed,
        "revenue": revenue,
        "accrued": partner.accrued(date_from, date_to),
        "paid_out": partner.paid_out(date_from, date_to),
        "balance": partner.balance(),
        "rows": rows,
        "funnel": funnel(students),
    }
