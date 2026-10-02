import csv
import json
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import services
from .backup import BackupError, import_backup, summary_lines
from .forms import (
    InstallmentForm, JobApplicationForm, NoteForm, PartnerPayoutForm, PaymentForm, StudentForm,
    staff_users,
)
from .models import (
    STAGE_GROUPS, Cohort, JobApplication, Partner, Payment, Stage, Student,
)
from .permissions import (
    admin_required, get_partner, is_admin, is_staff_member, staff_required, visible_students,
)


# ---------- Общее ----------

@login_required
def home(request):
    if is_staff_member(request.user):
        return redirect("dashboard")
    partner = get_partner(request.user)
    if partner:
        return redirect("partner_report", pk=partner.pk)
    raise PermissionDenied("У вашей учётной записи нет роли. Обратитесь к администратору.")


def apply_filters(request, qs):
    """Общие фильтры для доски, списка и дашборда: поток, ментор, партнёр, поиск."""
    f = {
        "cohort": request.GET.get("cohort", ""),
        "mentor": request.GET.get("mentor", ""),
        "partner": request.GET.get("partner", ""),
        "q": request.GET.get("q", "").strip(),
        "stage": request.GET.get("stage", ""),
    }
    if f["cohort"] == "none":
        qs = qs.filter(cohort__isnull=True)
    elif f["cohort"]:
        qs = qs.filter(cohort_id=f["cohort"])
    if f["mentor"]:
        qs = qs.filter(mentor_id=f["mentor"])
    if f["partner"]:
        qs = qs.filter(partner_id=f["partner"])
    if f["stage"]:
        qs = qs.filter(stage=f["stage"])
    if f["q"]:
        qs = qs.filter(
            Q(full_name__icontains=f["q"]) | Q(phone__icontains=f["q"])
            | Q(email__icontains=f["q"]) | Q(telegram__icontains=f["q"])
        )
    return qs, f


def filter_context(request):
    return {
        "cohorts": Cohort.objects.all(),
        "mentors": staff_users() if is_admin(request.user) else [],
        "partners": Partner.objects.all() if is_admin(request.user) else [],
        "stage_choices": Stage.choices,
    }


# ---------- Дашборд ----------

@staff_required
def dashboard(request):
    qs, f = apply_filters(request, visible_students(request.user))
    ctx = services.dashboard_metrics(qs)
    ctx.update(filter_context(request), f=f, active="dashboard")
    return render(request, "crm/dashboard.html", ctx)


# ---------- Канбан ----------

@staff_required
def board(request):
    qs, f = apply_filters(request, visible_students(request.user))
    show_closed = request.GET.get("closed") == "1"
    students = list(qs.prefetch_related("payments"))
    columns = []
    for stage in Stage:
        if stage == Stage.LOST and not show_closed:
            continue
        items = [s for s in students if s.stage == stage.value]
        items.sort(key=lambda s: s.stage_changed_at)
        group = next(g for g, st in STAGE_GROUPS.items() if stage in st)
        columns.append({"stage": stage.value, "label": stage.label, "group": group, "items": items})
    ctx = {"columns": columns, "f": f, "show_closed": show_closed, "active": "board"}
    ctx.update(filter_context(request))
    return render(request, "crm/board.html", ctx)


@staff_required
@require_POST
def board_move(request):
    try:
        data = json.loads(request.body)
        student = get_object_or_404(visible_students(request.user), pk=int(data["id"]))
        stage = data["stage"]
    except (ValueError, KeyError, TypeError):
        return HttpResponseBadRequest("bad request")
    if stage not in Stage.values:
        return HttpResponseBadRequest("unknown stage")
    student.stage = stage
    student.save(changed_by=request.user)
    return JsonResponse({"ok": True, "stage": stage, "label": Stage(stage).label})


# ---------- Студенты ----------

@staff_required
def student_list(request):
    qs, f = apply_filters(request, visible_students(request.user))
    if request.GET.get("export") == "csv" and is_admin(request.user):
        return export_csv(qs)
    ctx = {"students": qs.order_by("-created_at"), "f": f, "active": "students"}
    ctx.update(filter_context(request))
    return render(request, "crm/student_list.html", ctx)


def export_csv(qs):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="students.csv"'
    response.write("﻿")  # BOM, чтобы Excel корректно открыл кириллицу
    w = csv.writer(response, delimiter=";")
    w.writerow(["ФИО", "Телефон", "Telegram", "Email", "Этап", "Поток", "Ментор", "Партнёр",
                "Стоимость", "Оплачено", "Компания", "Зарплата", "Дата оффера", "Создан"])
    for s in qs:
        w.writerow([
            s.full_name, s.phone, s.telegram, s.email, s.get_stage_display(),
            s.cohort or "", (s.mentor.get_full_name() or s.mentor.username) if s.mentor else "",
            s.partner or "", s.price or "", s.total_paid, s.job_company, s.job_salary or "",
            s.offer_date or "", timezone.localtime(s.created_at).strftime("%d.%m.%Y"),
        ])
    return response


@staff_required
def student_create(request):
    form = StudentForm(request.POST or None, user=request.user)
    if not is_admin(request.user):
        form.fields["mentor"].initial = request.user.pk
    if request.method == "POST" and form.is_valid():
        student = form.save(commit=False)
        if not is_admin(request.user):
            student.mentor = request.user
        student.save(changed_by=request.user)
        messages.success(request, "Студент добавлен")
        return redirect(student)
    return render(request, "crm/student_form.html", {"form": form, "active": "students"})


@staff_required
def student_edit(request, pk):
    student = get_object_or_404(visible_students(request.user), pk=pk)
    form = StudentForm(request.POST or None, instance=student, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save(commit=False).save(changed_by=request.user)
        messages.success(request, "Сохранено")
        return redirect(student)
    return render(request, "crm/student_form.html", {"form": form, "student": student, "active": "students"})


@staff_required
def student_detail(request, pk):
    student = get_object_or_404(visible_students(request.user), pk=pk)
    ctx = {
        "s": student,
        "payments": student.payments.all(),
        "applications": student.applications.all(),
        "notes": student.notes.select_related("author"),
        "history": student.history.select_related("changed_by"),
        "note_form": NoteForm(),
        "payment_form": PaymentForm(initial={"due_date": timezone.localdate()}),
        "installment_form": InstallmentForm(initial={
            "total": student.debt or student.price or (student.cohort.price if student.cohort else None),
            "first_date": timezone.localdate(),
        }),
        "app_form": JobApplicationForm(),
        "stage_choices": Stage.choices,
        "active": "students",
    }
    return render(request, "crm/student_detail.html", ctx)


@staff_required
@require_POST
def student_stage(request, pk):
    student = get_object_or_404(visible_students(request.user), pk=pk)
    stage = request.POST.get("stage")
    if stage in Stage.values:
        student.stage = stage
        if stage == Stage.LOST and request.POST.get("lost_reason"):
            student.lost_reason = request.POST["lost_reason"]
        student.save(changed_by=request.user)
        messages.success(request, f"Этап: {Stage(stage).label}")
    return redirect(student)


@staff_required
@require_POST
def note_add(request, pk):
    student = get_object_or_404(visible_students(request.user), pk=pk)
    form = NoteForm(request.POST)
    if form.is_valid():
        note = form.save(commit=False)
        note.student, note.author = student, request.user
        note.save()
    return redirect(student)


# ---------- Платежи ----------

@staff_required
@require_POST
def payment_add(request, pk):
    student = get_object_or_404(visible_students(request.user), pk=pk)
    form = PaymentForm(request.POST)
    if form.is_valid():
        p = form.save(commit=False)
        p.student = student
        p.save()
        messages.success(request, "Платёж добавлен")
    else:
        messages.error(request, "Проверьте сумму и дату платежа")
    return redirect(student)


@staff_required
@require_POST
def installments_add(request, pk):
    student = get_object_or_404(visible_students(request.user), pk=pk)
    form = InstallmentForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте параметры рассрочки")
        return redirect(student)
    total, parts, first = (form.cleaned_data[k] for k in ("total", "parts", "first_date"))
    base = (total / parts).quantize(Decimal("0.01"))
    for i in range(parts):
        amount = base if i < parts - 1 else total - base * (parts - 1)  # остаток копеек — в последний платёж
        Payment.objects.create(
            student=student, amount=amount, due_date=first + relativedelta(months=i),
            comment=f"Рассрочка {i + 1}/{parts}",
        )
    messages.success(request, f"Создан график: {parts} платеж(а/ей)")
    return redirect(student)


def get_payment(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("student"), pk=pk)
    if not visible_students(request.user).filter(pk=payment.student_id).exists():
        raise PermissionDenied
    return payment


@staff_required
@require_POST
def payment_paid(request, pk):
    payment = get_payment(request, pk)
    payment.status = Payment.Status.PAID
    payment.paid_date = timezone.localdate()
    payment.save()
    messages.success(request, f"Платёж {payment.amount} отмечен оплаченным")
    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(payment.student)


@staff_required
def payment_edit(request, pk):
    payment = get_payment(request, pk)
    form = PaymentForm(request.POST or None, instance=payment)
    if request.method == "POST":
        if request.POST.get("delete"):
            student = payment.student
            payment.delete()
            messages.success(request, "Платёж удалён")
            return redirect(student)
        if form.is_valid():
            form.save()
            return redirect(payment.student)
    return render(request, "crm/simple_form.html", {
        "form": form, "title": f"Платёж — {payment.student}", "back": payment.student.get_absolute_url(),
        "can_delete": True, "active": "students",
    })


@admin_required
def payments(request):
    today = timezone.localdate()
    qs = Payment.objects.select_related("student", "student__cohort")
    tab = request.GET.get("tab", "overdue")
    if tab == "overdue":
        qs = qs.filter(status=Payment.Status.PLANNED, due_date__lt=today)
    elif tab == "upcoming":
        qs = qs.filter(status=Payment.Status.PLANNED, due_date__gte=today)
    else:
        tab = "paid"
        qs = qs.filter(status=Payment.Status.PAID).order_by("-paid_date")
    return render(request, "crm/payments.html", {
        "payments": qs, "tab": tab, "total": qs.aggregate(s=Sum("amount"))["s"] or 0,
        "active": "payments",
    })


# ---------- Трудоустройство ----------

@staff_required
@require_POST
def application_add(request, pk):
    student = get_object_or_404(visible_students(request.user), pk=pk)
    form = JobApplicationForm(request.POST)
    if form.is_valid():
        app = form.save(commit=False)
        app.student = student
        app.save(changed_by=request.user)
        messages.success(request, "Отклик добавлен")
    else:
        messages.error(request, "Укажите хотя бы компанию")
    return redirect(student)


@staff_required
def application_edit(request, pk):
    app = get_object_or_404(JobApplication.objects.select_related("student"), pk=pk)
    if not visible_students(request.user).filter(pk=app.student_id).exists():
        raise PermissionDenied
    form = JobApplicationForm(request.POST or None, instance=app)
    if request.method == "POST":
        if request.POST.get("delete"):
            app.delete()
            return redirect(app.student)
        if form.is_valid():
            form.save(commit=False).save(changed_by=request.user)
            return redirect(app.student)
    return render(request, "crm/simple_form.html", {
        "form": form, "title": f"Отклик — {app.student}", "back": app.student.get_absolute_url(),
        "can_delete": True, "active": "students",
    })


# ---------- Партнёры ----------

@admin_required
def partner_list(request):
    partners = Partner.objects.all()
    rows = [{"p": p, "leads": p.students.count(), "accrued": p.accrued(),
             "paid_out": p.paid_out(), "balance": p.balance()} for p in partners]
    return render(request, "crm/partner_list.html", {"rows": rows, "active": "partners"})


def parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@login_required
def partner_report(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    admin = is_admin(request.user)
    if not admin and get_partner(request.user) != partner:
        raise PermissionDenied
    date_from = parse_date(request.GET.get("from"))
    date_to = parse_date(request.GET.get("to"))
    ctx = services.partner_report(partner, date_from, date_to)
    ctx.update({
        "partner": partner, "is_admin_view": admin, "date_from": date_from, "date_to": date_to,
        "payouts": partner.payouts.all(), "payout_form": PartnerPayoutForm(),
        "active": "partners",
    })
    return render(request, "crm/partner_report.html", ctx)


@admin_required
@require_POST
def payout_add(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    form = PartnerPayoutForm(request.POST)
    if form.is_valid():
        payout = form.save(commit=False)
        payout.partner = partner
        payout.save()
        messages.success(request, "Выплата записана")
    return redirect("partner_report", pk=pk)


# ---------- Импорт резервной копии из веб-версии ----------

@admin_required
def backup_import(request):
    ctx = {"active": "settings"}
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            ctx["error"] = "Выберите файл копии"
        elif upload.size > 50 * 1024 * 1024:
            ctx["error"] = "Файл больше 50 МБ — это не похоже на копию CRM"
        else:
            try:
                stats = import_backup(
                    upload.read(), overwrite=bool(request.POST.get("overwrite")),
                    with_demo=bool(request.POST.get("with_demo")),
                )
                ctx["result"], ctx["warnings"] = summary_lines(stats), stats["warnings"]
            except BackupError as exc:
                ctx["error"] = str(exc)
    return render(request, "crm/backup_import.html", ctx)


# ---------- Приём заявок с лендинга ----------

@csrf_exempt
@require_POST
def api_lead(request):
    """Webhook для форм (Tilda, Taplink, свой сайт).

    POST /api/leads/?token=<LEAD_WEBHOOK_TOKEN>
    Поля (form-data или JSON): name, phone, email, telegram, city,
    utm_source, utm_campaign, promo (или promo_code), consent.
    """
    token = request.GET.get("token") or request.headers.get("X-Token", "")
    if not settings.LEAD_WEBHOOK_TOKEN or token != settings.LEAD_WEBHOOK_TOKEN:
        return JsonResponse({"error": "forbidden"}, status=403)

    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or "{}")
        except ValueError:
            return JsonResponse({"error": "bad json"}, status=400)
    else:
        data = request.POST

    def get(*keys):
        for k in keys:
            for variant in (k, k.capitalize(), k.upper()):
                v = data.get(variant)
                if v:
                    return str(v).strip()[:200]
        return ""

    # Tilda при сохранении настроек webhook шлёт тестовый запрос test=test
    if get("test") == "test":
        return JsonResponse({"ok": True, "test": True})

    name = get("name", "full_name", "fio")
    phone = get("phone", "tel")
    email = get("email")
    telegram = get("telegram", "tg")
    if not (name or phone or email or telegram):
        return JsonResponse({"error": "empty lead"}, status=400)

    utm_source = get("utm_source")
    promo = get("promo", "promo_code", "promocode")
    partner = None
    if promo:
        partner = Partner.objects.filter(promo_code__iexact=promo, is_active=True).first()
    if not partner and utm_source:
        partner = Partner.objects.filter(utm_source__iexact=utm_source, is_active=True).first()

    # Повторная заявка того же человека не плодит дубли
    existing = None
    if phone:
        existing = Student.objects.filter(phone=phone).first()
    if not existing and email:
        existing = Student.objects.filter(email__iexact=email).first()
    if existing:
        existing.notes.create(text=f"Повторная заявка с сайта (utm_source={utm_source or '—'})")
        return JsonResponse({"ok": True, "id": existing.pk, "duplicate": True})

    student = Student(
        full_name=name or phone or email or telegram,
        phone=phone, email=email, telegram=telegram, city=get("city"),
        utm_source=utm_source, utm_campaign=get("utm_campaign"), promo_code=promo,
        partner=partner, consent_pd=bool(get("consent", "agreement")),
    )
    active_cohort = Cohort.objects.filter(is_active=True).order_by("start_date").first()
    if active_cohort:
        student.cohort = active_cohort
    student.save()
    return JsonResponse({"ok": True, "id": student.pk}, status=201)
