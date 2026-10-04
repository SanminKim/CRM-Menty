"""Подключает Telegram-бота: сообщает Telegram адрес сервера и запоминает имя бота.

Запускается сама при каждом старте сервера. Без TELEGRAM_BOT_TOKEN ничего не делает.
"""
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from backend import store, telegram
from backend.models import TgChat


class Command(BaseCommand):
    help = "Настраивает webhook Telegram-бота"

    def handle(self, *args, **opts):
        # Разговоры, брошенные на полпути больше месяца назад, не храним: в них могут быть имя и телефон
        stale = timezone.now() - timedelta(days=30)
        TgChat.objects.filter(user__isnull=True, updated_at__lt=stale).exclude(state="done").delete()
        if not telegram.enabled():
            self.stdout.write("Telegram-бот выключен: TELEGRAM_BOT_TOKEN не задан.")
            return
        cache.delete("tg-down")
        me = telegram.call("getMe")
        if not me or not me.get("username"):
            self.stderr.write("Telegram не принял токен бота. Проверьте TELEGRAM_BOT_TOKEN в .env.")
            return
        st = store.state()
        st.bot_username = me["username"][:64]
        st.save(update_fields=["bot_username"])
        ok = telegram.call(
            "setWebhook", url=f"{settings.PUBLIC_URL}/api/telegram/", secret_token=telegram.webhook_secret(),
            allowed_updates=["message", "callback_query"], max_connections=2,  # не больше двух сообщений одновременно
        )
        telegram.call("setMyCommands", commands=[{"command": "start", "description": "Оставить заявку"}])
        if ok:
            self.stdout.write(f"Бот @{me['username']} подключён: {settings.PUBLIC_URL}/api/telegram/")
        else:
            self.stderr.write("Не удалось сообщить Telegram адрес сервера. Адрес должен открываться по https.")
