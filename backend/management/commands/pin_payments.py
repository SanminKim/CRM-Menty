"""Отмечает у уже оплаченных платежей направление, в котором получены деньги (dirId).

Новые оплаты отмечаются при записи сами. Команда нужна для платежей, оплаченных раньше: берётся направление потока
студента на момент запуска. Повторный запуск ничего не меняет. Запускается при каждом старте контейнера.
"""
import copy

from django.core.management.base import BaseCommand

from backend import reports, store
from backend.models import Doc


class Command(BaseCommand):
    help = "Отметить направление у оплаченных платежей без отметки"

    def handle(self, *args, **options):
        changed, rev = 0, None
        for doc in Doc.objects.filter(collection="students").order_by("doc_id"):
            data = doc.data if isinstance(doc.data, dict) else None
            pays = data.get("payments") if data else None
            if not isinstance(pays, list):
                continue
            if not any(isinstance(p, dict) and reports._day(p.get("paid")) and not isinstance(p.get("dirId"), str) for p in pays):
                continue
            rev = store.system_write("students", doc.doc_id, copy.deepcopy(data), refresh=False)  # запись сама отмечает направление
            changed += 1
        if rev is not None:
            reports.refresh(rev)  # отчёты пересчитываются один раз за весь проход
        self.stdout.write(f"Отмечено направление у платежей студентов: {changed}")
