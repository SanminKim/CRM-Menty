from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone


class Stage(models.TextChoices):
    """Этапы пути студента: от заявки до прохождения испытательного срока."""

    NEW = "new", "Новая заявка"
    CONTACTED = "contacted", "Связались"
    CALL = "call", "Созвон"
    WAITING_PAYMENT = "waiting_payment", "Ждём оплату"
    STUDYING = "studying", "Обучается"
    FINAL_PROJECT = "final_project", "Итоговый проект"
    JOB_SEARCH = "job_search", "Поиск работы"
    OFFER = "offer", "Оффер"
    PROBATION_PASSED = "probation_passed", "Прошёл испытательный"
    LOST = "lost", "Отказ / выбыл"


# Группы этапов — для цветов на доске и расчёта метрик
STAGE_GROUPS = {
    "sales": [Stage.NEW, Stage.CONTACTED, Stage.CALL, Stage.WAITING_PAYMENT],
    "study": [Stage.STUDYING, Stage.FINAL_PROJECT],
    "job": [Stage.JOB_SEARCH, Stage.OFFER, Stage.PROBATION_PASSED],
    "closed": [Stage.LOST],
}
STAGE_ORDER = [s.value for s in Stage]
PAID_STAGES = [s.value for s in STAGE_GROUPS["study"] + STAGE_GROUPS["job"]]
EMPLOYED_STAGES = [Stage.OFFER.value, Stage.PROBATION_PASSED.value]


def stage_group(stage):
    for group, stages in STAGE_GROUPS.items():
        if stage in stages:
            return group
    return "sales"


class Partner(models.Model):
    """Партнёр-источник трафика (блогер). Получает долю от оплат своих студентов."""

    name = models.CharField("Имя", max_length=200)
    promo_code = models.CharField("Промокод", max_length=50, blank=True)
    utm_source = models.CharField(
        "utm_source", max_length=100, blank=True,
        help_text="Заявки с этим utm_source автоматически привяжутся к партнёру",
    )
    share_percent = models.DecimalField(
        "Доля партнёра, %", max_digits=5, decimal_places=2, default=Decimal("50")
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="Учётная запись", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="partner",
        help_text="Если указать — партнёр сможет входить и видеть отчёт по своему трафику",
    )
    contacts = models.TextField("Контакты / реквизиты", blank=True)
    external_id = models.CharField(
        "ID в веб-версии", max_length=64, blank=True, db_index=True, editable=False,
        help_text="Заполняется при импорте резервной копии, чтобы не создавать дубли",
    )
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Партнёр"
        verbose_name_plural = "Партнёры"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def accrued(self, date_from=None, date_to=None):
        """Сколько начислено партнёру по фактическим оплатам его студентов."""
        payments = Payment.objects.filter(
            student__partner=self, status=Payment.Status.PAID
        ).select_related("student")
        if date_from:
            payments = payments.filter(paid_date__gte=date_from)
        if date_to:
            payments = payments.filter(paid_date__lte=date_to)
        total = Decimal("0")
        for p in payments:
            total += p.amount * p.student.partner_share_percent / Decimal("100")
        return total.quantize(Decimal("0.01"))

    def paid_out(self, date_from=None, date_to=None):
        qs = self.payouts.all()
        if date_from:
            qs = qs.filter(paid_date__gte=date_from)
        if date_to:
            qs = qs.filter(paid_date__lte=date_to)
        return qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    def balance(self):
        """Сколько ещё должны партнёру за всё время."""
        return self.accrued() - self.paid_out()


class Cohort(models.Model):
    """Поток обучения."""

    name = models.CharField("Название", max_length=100)
    start_date = models.DateField("Старт", null=True, blank=True)
    end_date = models.DateField("Окончание обучения", null=True, blank=True)
    price = models.DecimalField(
        "Базовая цена", max_digits=12, decimal_places=2, null=True, blank=True
    )
    is_active = models.BooleanField("Идёт набор / обучение", default=True)
    external_id = models.CharField(
        "ID в веб-версии", max_length=64, blank=True, db_index=True, editable=False,
        help_text="Заполняется при импорте резервной копии, чтобы не создавать дубли",
    )

    class Meta:
        verbose_name = "Поток"
        verbose_name_plural = "Потоки"
        ordering = ["-start_date", "name"]

    def __str__(self):
        return self.name


class Student(models.Model):
    """Человек на всём пути: лид → студент → выпускник с оффером."""

    # Контакты
    full_name = models.CharField("ФИО", max_length=200)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    telegram = models.CharField("Telegram", max_length=100, blank=True)
    email = models.EmailField("Email", blank=True)
    city = models.CharField("Город", max_length=100, blank=True)
    consent_pd = models.BooleanField("Согласие на обработку ПД", default=False)

    # Откуда пришёл
    partner = models.ForeignKey(
        Partner, verbose_name="Партнёр (источник)", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="students",
    )
    partner_share_percent = models.DecimalField(
        "Доля партнёра, %", max_digits=5, decimal_places=2, default=Decimal("0"),
        help_text="Фиксируется при привязке партнёра, чтобы смена условий не пересчитывала старые сделки",
    )
    utm_source = models.CharField("utm_source", max_length=100, blank=True)
    utm_campaign = models.CharField("utm_campaign", max_length=200, blank=True)
    promo_code = models.CharField("Промокод", max_length=50, blank=True)

    # Путь
    stage = models.CharField("Этап", max_length=30, choices=Stage.choices, default=Stage.NEW)
    stage_changed_at = models.DateTimeField("Этап изменён", default=timezone.now)
    lost_reason = models.CharField("Причина отказа / выбытия", max_length=255, blank=True)
    cohort = models.ForeignKey(
        Cohort, verbose_name="Поток", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="students",
    )
    mentor = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Ментор", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="mentees",
    )
    price = models.DecimalField(
        "Стоимость обучения", max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Итоговая цена по договору с учётом скидок",
    )

    # Результат
    job_company = models.CharField("Компания (оффер)", max_length=200, blank=True)
    job_position = models.CharField("Должность", max_length=200, blank=True)
    job_salary = models.DecimalField(
        "Зарплата на оффере", max_digits=12, decimal_places=2, null=True, blank=True
    )
    offer_date = models.DateField("Дата оффера", null=True, blank=True)

    next_step = models.CharField("Следующий шаг", max_length=255, blank=True)
    next_step_date = models.DateField("Дата следующего шага", null=True, blank=True)

    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField("Создан", default=timezone.now)
    external_id = models.CharField(
        "ID в веб-версии", max_length=64, blank=True, db_index=True, editable=False,
        help_text="Заполняется при импорте резервной копии, чтобы не создавать дубли",
    )
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Студент"
        verbose_name_plural = "Студенты"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["stage"]), models.Index(fields=["created_at"])]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_stage = self.stage
        # Для новой записи «исходного» партнёра нет — доля зафиксируется при первом сохранении
        self._original_partner_id = self.partner_id if self.pk else None

    def __str__(self):
        return self.full_name

    def get_absolute_url(self):
        return reverse("student_detail", args=[self.pk])

    def save(self, *args, changed_by=None, **kwargs):
        # Фиксируем долю партнёра при первой привязке / смене партнёра
        if self.partner_id and self.partner_id != self._original_partner_id:
            self.partner_share_percent = self.partner.share_percent
        if not self.partner_id:
            self.partner_share_percent = Decimal("0")

        is_new = self.pk is None
        stage_changed = not is_new and self.stage != self._original_stage
        if stage_changed:
            self.stage_changed_at = timezone.now()
        super().save(*args, **kwargs)

        if is_new or stage_changed:
            StageHistory.objects.create(
                student=self,
                from_stage="" if is_new else self._original_stage,
                to_stage=self.stage,
                changed_by=changed_by,
            )
        self._original_stage = self.stage
        self._original_partner_id = self.partner_id

    # --- Вычисляемые свойства для карточки и доски ---
    @property
    def stage_group(self):
        return stage_group(self.stage)

    @property
    def days_in_stage(self):
        return (timezone.now() - self.stage_changed_at).days

    @property
    def is_stuck(self):
        active = self.stage not in (Stage.LOST, Stage.PROBATION_PASSED, Stage.NEW)
        return active and self.days_in_stage >= settings.CRM_STUCK_DAYS

    @property
    def total_paid(self):
        return self.payments.filter(status=Payment.Status.PAID).aggregate(
            s=Sum("amount"))["s"] or Decimal("0")

    @property
    def debt(self):
        """Остаток к оплате по договору."""
        if self.price is None:
            return None
        return max(self.price - self.total_paid, Decimal("0"))

    @property
    def has_overdue(self):
        return self.payments.filter(
            status=Payment.Status.PLANNED, due_date__lt=timezone.localdate()
        ).exists()


class StageHistory(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="history")
    from_stage = models.CharField(max_length=30, choices=Stage.choices, blank=True)
    to_stage = models.CharField(max_length=30, choices=Stage.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Смена этапа"
        verbose_name_plural = "История этапов"
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.student}: {self.get_from_stage_display()} → {self.get_to_stage_display()}"


class Payment(models.Model):
    """Платёж студента: плановый (график рассрочки) или фактический."""

    class Status(models.TextChoices):
        PLANNED = "planned", "Ожидается"
        PAID = "paid", "Оплачен"
        CANCELLED = "cancelled", "Отменён"

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)
    due_date = models.DateField("Срок оплаты")
    paid_date = models.DateField("Дата оплаты", null=True, blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.PLANNED)
    method = models.CharField("Способ оплаты", max_length=100, blank=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)

    class Meta:
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"
        ordering = ["due_date"]

    def __str__(self):
        return f"{self.student} — {self.amount} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if self.status == self.Status.PAID and not self.paid_date:
            self.paid_date = timezone.localdate()
        if self.status != self.Status.PAID:
            self.paid_date = None
        super().save(*args, **kwargs)

    @property
    def is_overdue(self):
        return self.status == self.Status.PLANNED and self.due_date < timezone.localdate()


class PartnerPayout(models.Model):
    """Выплата партнёру его доли."""

    partner = models.ForeignKey(Partner, on_delete=models.CASCADE, related_name="payouts")
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)
    paid_date = models.DateField("Дата выплаты", default=timezone.localdate)
    external_id = models.CharField(
        "ID в веб-версии", max_length=64, blank=True, db_index=True, editable=False,
        help_text="Заполняется при импорте резервной копии, чтобы не создавать дубли",
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)

    class Meta:
        verbose_name = "Выплата партнёру"
        verbose_name_plural = "Выплаты партнёрам"
        ordering = ["-paid_date"]

    def __str__(self):
        return f"{self.partner}: {self.amount} ({self.paid_date})"


class JobApplication(models.Model):
    """Отклик студента на вакансию и его движение до оффера."""

    class Status(models.TextChoices):
        APPLIED = "applied", "Отклик"
        HR = "hr", "Скрининг HR"
        TEST = "test", "Тестовое"
        INTERVIEW = "interview", "Собеседование"
        OFFER = "offer", "Оффер"
        ACCEPTED = "accepted", "Оффер принят"
        REJECTED = "rejected", "Отказ"

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="applications")
    company = models.CharField("Компания", max_length=200)
    position = models.CharField("Вакансия", max_length=200, blank=True)
    url = models.URLField("Ссылка", blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.APPLIED)
    salary = models.DecimalField("Зарплата", max_digits=12, decimal_places=2, null=True, blank=True)
    applied_date = models.DateField("Дата отклика", default=timezone.localdate)
    interview_at = models.DateTimeField("Собеседование", null=True, blank=True)
    notes = models.TextField("Заметки", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Отклик"
        verbose_name_plural = "Отклики и собеседования"
        ordering = ["-applied_date", "-id"]

    def __str__(self):
        return f"{self.student} → {self.company}"

    def save(self, *args, changed_by=None, **kwargs):
        super().save(*args, **kwargs)
        # Принятый оффер автоматически переносит студента на этап «Оффер»
        if self.status == self.Status.ACCEPTED:
            s = self.student
            s.job_company = self.company
            s.job_position = self.position
            if self.salary:
                s.job_salary = self.salary
            s.offer_date = s.offer_date or timezone.localdate()
            if STAGE_ORDER.index(s.stage) < STAGE_ORDER.index(Stage.OFFER):
                s.stage = Stage.OFFER
            s.save(changed_by=changed_by)


class Note(models.Model):
    """Заметка в ленте студента: созвон, фидбэк по домашке, договорённость."""

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    text = models.TextField("Текст")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Заметка"
        verbose_name_plural = "Заметки"
        ordering = ["-created_at"]

    def __str__(self):
        return self.text[:50]
