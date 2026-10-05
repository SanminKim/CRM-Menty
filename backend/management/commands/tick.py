"""Дела по расписанию: python manage.py tick --loop (так его запускает служба scheduler в docker-compose)."""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from backend import scheduler

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Утренняя сводка, напоминания о встречах и копия базы в Telegram"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="работать постоянно, проверяя дела раз в минуту")

    def handle(self, *args, loop, **opts):
        while True:
            close_old_connections()  # соединение с базой могло закрыться, пока планировщик спал
            try:
                scheduler.run()
            except Exception:  # база недоступна или ещё не готова: пробуем через минуту
                log.exception("Планировщик: проход не выполнен")
            if not loop:
                return
            close_old_connections()
            time.sleep(60)
