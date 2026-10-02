"""Импорт резервной копии из веб-версии: python manage.py import_backup копия.json"""
from django.core.management.base import BaseCommand, CommandError

from crm.backup import BackupError, import_backup, summary_lines


class Command(BaseCommand):
    help = "Перенести данные из резервной копии веб-версии CRM"

    def add_arguments(self, parser):
        parser.add_argument("path", help="Путь к файлу копии (.json)")
        parser.add_argument("--overwrite", action="store_true",
                            help="Заменить уже перенесённых студентов данными из копии")
        parser.add_argument("--with-demo", action="store_true",
                            help="Переносить и записи с пометкой «пример»")

    def handle(self, *args, **opts):
        try:
            with open(opts["path"], "rb") as fh:
                stats = import_backup(fh.read(), overwrite=opts["overwrite"], with_demo=opts["with_demo"])
        except OSError as exc:
            raise CommandError(f"Не удалось прочитать файл: {exc}") from exc
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        for line in summary_lines(stats):
            self.stdout.write(line)
        for warning in stats["warnings"]:
            self.stdout.write(self.style.WARNING(warning))
        self.stdout.write(self.style.SUCCESS("Импорт завершён"))
