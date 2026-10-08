"""Проверка, что CRM работает, и сигнал наружу, если нет.

- `/health/` — открытый адрес без входа: «ok» и код 200, если отвечают сервер, база и планировщик; иначе 503 и что не так.
- Планировщик раз в 5 минут открывает `/health/` по внешнему адресу CRM (через домен, HTTPS и Caddy — тем же путём,
  что и сотрудники) и сообщает результат внешнему сервису Healthchecks.io по ссылке из `HEALTHCHECK_PING_URL`.
  Если сообщения перестали приходить (сервер выключен, сайт не открывается, планировщик остановился) или пришло
  «не в порядке», Healthchecks.io пишет администратору в Telegram. Свой бот для этого не годится: он работает
  на том же сервере и упадёт вместе с ним.
"""
import datetime
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)

BEAT_KEY = "health:scheduler"
BEAT_MAX_AGE = datetime.timedelta(minutes=15)   # планировщик проходит раз в минуту; запас на перезапуск после выкладки
WATCH_KEY = "health:watch"
WATCH_SECONDS = 290                             # раз в 5 минут, с запасом на минутный шаг планировщика
TIMEOUT = 15


def beat(now=None):
    """Планировщик жив: отметка ставится на каждом его проходе."""
    cache.set(BEAT_KEY, (now or timezone.now()).isoformat(), 3600)


def problems(now=None):
    """Что не работает. Пустой список — всё в порядке."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        stamp = cache.get(BEAT_KEY)
    except Exception:  # база недоступна: дальше проверять нечего
        return ["база данных не отвечает"]
    try:
        alive = stamp and (now or timezone.now()) - datetime.datetime.fromisoformat(stamp) <= BEAT_MAX_AGE
    except (TypeError, ValueError):
        alive = False
    return [] if alive else ["планировщик не работает: не уходят сводки, напоминания и копии базы"]


@never_cache
@require_GET
def view(request):
    found = problems()
    response = HttpResponse("ok" if not found else "Не в порядке: " + "; ".join(found),
                            status=200 if not found else 503, content_type="text/plain; charset=utf-8")
    response["X-Robots-Tag"] = "noindex"
    return response


def _open(url, data=None):
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "crm-menti-health"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.status, response.read(500).decode("utf-8", "replace")


def check_public():
    """Открывает /health/ по внешнему адресу CRM. (True, "") или (False, причина)."""
    try:
        status, text = _open(f"{settings.PUBLIC_URL}/health/")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(500).decode("utf-8", "replace")
        except Exception:
            text = ""
        return False, f"сайт ответил с ошибкой {exc.code}. {text}".strip()
    except Exception as exc:  # нет связи, не открылся домен, истёк сертификат
        return False, f"сайт не открывается снаружи ({type(exc).__name__})"
    return (True, "") if status == 200 and text.strip() == "ok" else (False, f"сайт ответил {status}: {text[:200]}")


def watch(now=None):
    """Раз в 5 минут: проверить сайт снаружи и сообщить результат Healthchecks.io. Без ссылки ничего не делает."""
    url = settings.HEALTHCHECK_PING_URL
    if not url or not cache.add(WATCH_KEY, 1, WATCH_SECONDS):
        return
    ok, reason = check_public()
    try:
        _open(url if ok else url + "/fail", data=(reason or "ok").encode()[:1000])
    except Exception as exc:  # Healthchecks.io недоступен: молчание само станет сигналом
        log.warning("Проверка работы: сигнал не отправлен (%s)", type(exc).__name__)
