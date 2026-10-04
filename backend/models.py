"""Серверное хранилище CRM.

Страница (web/index.html) работает с данными как с документами в коллекциях: студент со всеми
платежами, откликами и заметками — один документ. Сервер хранит эти документы как есть, проверяет,
кто что может читать и писать, и отдаёт изменения по номеру ревизии.
"""
from django.conf import settings
from django.db import models

COLLECTIONS = ("students", "cohorts", "partners", "payouts", "team", "reports", "meetings", "config")


class State(models.Model):
    """Одна строка на всю базу: счётчик ревизий и «эпоха» прав доступа."""

    rev = models.BigIntegerField(default=0)
    # Меняется, когда меняются учётные записи: клиенты перечитывают базу целиком
    epoch = models.IntegerField(default=1)
    # Имя Telegram-бота: узнаётся у Telegram при настройке, нужно странице для ссылок
    bot_username = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "Состояние базы"
        verbose_name_plural = "Состояние базы"

    def __str__(self):
        return f"ревизия {self.rev}, эпоха {self.epoch}"


class Doc(models.Model):
    collection = models.CharField("Коллекция", max_length=20)
    doc_id = models.CharField("Идентификатор", max_length=64)
    data = models.JSONField("Данные", default=dict)
    rev = models.BigIntegerField("Ревизия", db_index=True)
    updated_at = models.DateTimeField("Изменён", auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто изменил", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )

    class Meta:
        verbose_name = "Запись"
        verbose_name_plural = "Записи"
        constraints = [models.UniqueConstraint(fields=["collection", "doc_id"], name="doc_unique")]

    def __str__(self):
        return f"{self.collection}/{self.doc_id}"


class Tombstone(models.Model):
    """Отметка об удалённом документе, чтобы открытые страницы убрали его у себя."""

    collection = models.CharField(max_length=20)
    doc_id = models.CharField(max_length=64)
    rev = models.BigIntegerField(db_index=True)

    class Meta:
        verbose_name = "Удалённая запись"
        verbose_name_plural = "Удалённые записи"
        indexes = [models.Index(fields=["collection", "doc_id"], name="tombstone_doc")]

    def __str__(self):
        return f"{self.collection}/{self.doc_id}"


class Account(models.Model):
    """Роль пользователя и то, с какой записью он связан."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Администратор"
        MENTOR = "mentor", "Ментор"
        PARTNER = "partner", "Партнёр"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="account")
    role = models.CharField("Роль", max_length=10, choices=Role.choices)
    link_id = models.CharField(
        "Связанная запись", max_length=64, blank=True,
        help_text="Для ментора и администратора — запись из списка команды, для партнёра — запись партнёра",
    )

    class Meta:
        verbose_name = "Доступ"
        verbose_name_plural = "Доступы"

    def __str__(self):
        return f"{self.user.get_username()} ({self.get_role_display()})"


class LeadLog(models.Model):
    """Журнал обращений к приёму заявок: помогает разобраться, если заявка с сайта не дошла."""

    created_at = models.DateTimeField(auto_now_add=True)
    ok = models.BooleanField(default=False)
    result = models.CharField(max_length=40)
    student_id = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name = "Обращение формы заявок"
        verbose_name_plural = "Обращения формы заявок"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%d.%m.%Y %H:%M} {self.result}"


class TgChat(models.Model):
    """Личный чат с Telegram-ботом: человек, оставляющий заявку, или сотрудник, получающий уведомления."""

    chat_id = models.BigIntegerField(unique=True)
    username = models.CharField(max_length=64, blank=True)
    first_name = models.CharField(max_length=100, blank=True)
    # Шаг разговора о заявке: consent → name → contact → goal → done
    state = models.CharField(max_length=10, blank=True)
    data = models.JSONField(default=dict, blank=True)
    student_id = models.CharField(max_length=64, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="tg_chats")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Чат Telegram"
        verbose_name_plural = "Чаты Telegram"

    def __str__(self):
        return f"{self.chat_id} {self.username}".strip()
