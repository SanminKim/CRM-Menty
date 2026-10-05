"""Загрузка резервной копии из консоли: python manage.py import_backup копия.json"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backend import store
from backend.backup import BackupError, import_backup, summary_lines


class Command(BaseCommand):
    help = "Загружает резервную копию, скачанную на странице CRM"

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--overwrite", action="store_true", help="заменить уже существующие записи")
        parser.add_argument("--with-demo", action="store_true", help="загрузить и примерные данные")

    def handle(self, *args, path, overwrite, with_demo, **opts):
        try:
            with open(path, "rb") as fh:
                # Зашифрованная копия из Telegram открывается паролем копии из настроек сервера
                stats = import_backup(fh.read(), overwrite=overwrite, with_demo=with_demo,
                                      passphrase=settings.BACKUP_PASSPHRASE or None)
        except OSError as exc:
            raise CommandError(f"Не удалось открыть файл: {exc}")
        except BackupError as exc:
            raise CommandError(str(exc))
        except store.Invalid as exc:
            raise CommandError(f"В копии есть запись, которую нельзя сохранить: {exc}")
        for line in summary_lines(stats):
            self.stdout.write(line)
