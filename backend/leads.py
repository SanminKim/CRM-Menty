"""Приём заявок: с сайта (POST /api/leads/ с токеном) и из Telegram-бота (backend/telegram.py)."""
import hmac
import json
import secrets

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import store
from .models import Doc, LeadLog

LEADS_PER_MINUTE = 30
MAX_AUTO_NOTES = 200
TRUE_WORDS = ("1", "true", "yes", "on", "да", "y")
SOURCE_NAMES = {"site": "с сайта", "telegram": "из Telegram"}


def new_id():
    return secrets.token_urlsafe(15)


def stamp():
    """Время в том же виде, в каком его пишет страница."""
    moment = timezone.now()
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())[-10:]


def _low(value):
    return str(value or "").strip().lower().lstrip("@")


def find_duplicate(phone, email, telegram, tg_id=None):
    """Тот же человек: (запись, чем совпал) или (None, ""). Корзина тоже учитывается.

    «tg» — совпал сам чат Telegram, это точно тот же человек. «contact» — совпал телефон, почта или
    имя пользователя, которые человек мог ввести и чужие.
    """
    by_contact = None
    for doc in Doc.objects.filter(collection="students"):
        d = doc.data
        if tg_id and d.get("tgId") == tg_id:
            return doc, "tg"
        if by_contact is not None:
            continue
        if ((phone and len(_digits(phone)) >= 6 and _digits(d.get("phone")) == _digits(phone))
                or (email and _low(d.get("email")) == _low(email))
                or (telegram and _low(d.get("telegram")) == _low(telegram))):
            by_contact = doc
    return (by_contact, "contact") if by_contact is not None else (None, "")


def plain(text, limit=100):
    """Текст от постороннего человека для уведомления: одной строкой и без ссылок, чтобы его нельзя было выдать за сообщение CRM."""
    text = " ".join(str(text or "").split())
    return text.replace("://", " ").replace("www.", " ").replace(".", "․")[:limit]


def next_cohort_id():
    """Ближайший по дате старта активный поток, как на странице."""
    active = [d for d in Doc.objects.filter(collection="cohorts") if d.data.get("active") is not False]
    today = timezone.localdate().isoformat()
    upcoming = sorted((d for d in active if not d.data.get("start") or d.data["start"] >= today),
                      key=lambda d: d.data.get("start") or "9999")
    if upcoming:
        return upcoming[0].doc_id
    started = sorted(active, key=lambda d: d.data.get("start") or "")
    return started[-1].doc_id if started else None


def find_partner(promo, utm):
    for doc in Doc.objects.filter(collection="partners"):
        d = doc.data
        if d.get("active") is False:
            continue
        if (promo and _low(d.get("promo")) == _low(promo)) or (utm and _low(d.get("utm")) == _low(utm)):
            return doc
    return None


def _log(ok, result, student_id=""):
    LeadLog.objects.create(ok=ok, result=result, student_id=student_id)


def _notify(text, student_id=None):
    from . import telegram  # позднее подключение: telegram сам пользуется этим модулем
    telegram.notify_admins(text, student_id=student_id)


def register(*, name="", phone="", email="", telegram="", city="", consent=False, promo="", utm="", campaign="",
             comment="", source="site", tg_id=None):
    """Создаёт заявку или, если человек уже есть в базе, добавляет заметку в его карточку.

    Возвращает (id студента, новая ли это заявка, чем совпал дубль). Поиск дубля и запись идут под одной
    блокировкой, поэтому двойная отправка формы не создаст двоих.
    """
    now = stamp()
    origin = SOURCE_NAMES.get(source, source)
    with transaction.atomic():
        store.lock()
        existing, matched = find_duplicate(phone, email, telegram, tg_id)
        if existing:
            # Повторная заявка не плодит дубль: в карточке появляется заметка
            notes = list(existing.data.get("notes") or [])
            if len(notes) < MAX_AUTO_NOTES:
                text = f"Повторная заявка {origin}" + (f": {comment}" if comment else "")
                notes.append({"id": new_id(), "text": text, "at": now, "by": None, "kind": "step"})
                store.system_write("students", existing.doc_id, {**existing.data, "notes": notes})
            _log(True, "duplicate", existing.doc_id)
            return existing.doc_id, False, matched
        partner = find_partner(promo, utm)
        doc_id = new_id()
        data = {
            "name": name or phone or email or telegram or "Без имени", "phone": phone, "telegram": telegram,
            "email": email, "city": city, "consent": bool(consent),
            "stage": "new", "stageAt": now, "createdAt": now,
            "history": [{"from": None, "to": "new", "at": now, "by": None}],
            "cohortId": next_cohort_id(), "mentorId": None, "price": None, "lostReason": "",
            "partnerId": partner.doc_id if partner else None,
            "partnerShare": (partner.data.get("share") or 0) if partner else 0,
            "promo": promo, "utmSource": utm, "utmCampaign": campaign,
            "jobCompany": "", "jobPosition": "", "jobSalary": None, "offerDate": None, "comment": comment,
            "next": {"text": "Связаться", "date": timezone.localdate().isoformat()},
            "payments": [], "apps": [], "notes": [], "source": source,
        }
        if tg_id:
            data["tgId"] = tg_id
        store.system_write("students", doc_id, data)
        _log(True, "created", doc_id)
        label = plain(data["name"]) + (f", {plain(telegram, 40)}" if telegram and telegram != data["name"] else "")
        text = f"🆕 Новая заявка на обучение {origin}: {label}" + (f" · {plain(partner.data.get('name'), 60)}" if partner else "")
        # Уведомление уходит после фиксации записи: сбой Telegram не отменяет заявку
        transaction.on_commit(lambda: _notify(text, doc_id))
    return doc_id, True, ""


def token_ok(token):
    """Токен из настроек. Короткий или оставленный из примера токен считается незаданным."""
    expected = settings.LEAD_WEBHOOK_TOKEN
    if len(expected) < 16 or expected.startswith("change-me"):
        return False
    return hmac.compare_digest(token.encode(), expected.encode())


def _too_many():
    key = "leads-minute"
    count = (cache.get(key) or 0) + 1
    cache.set(key, count, 60)
    return count > LEADS_PER_MINUTE


@csrf_exempt
@require_POST
def api_lead(request):
    token = request.headers.get("X-Token") or request.GET.get("token") or ""
    if not token_ok(token):
        return JsonResponse({"error": "forbidden"}, status=403)
    if _too_many():
        return JsonResponse({"error": "too many requests"}, status=429)

    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except (ValueError, RecursionError):
            return JsonResponse({"error": "bad json"}, status=400)
        if not isinstance(data, dict):
            return JsonResponse({"error": "bad json"}, status=400)
    else:
        data = request.POST

    def get(*keys):
        for key in keys:
            for variant in (key, key.capitalize(), key.upper()):
                value = data.get(variant)
                if value and isinstance(value, (str, int, float)):
                    return str(value).replace("\x00", "").strip()[:200]
        return ""

    # Tilda при сохранении настроек webhook шлёт тестовый запрос test=test
    if get("test") == "test":
        return JsonResponse({"ok": True, "test": True})

    name, phone, email = get("name", "full_name", "fio"), get("phone", "tel"), get("email")
    telegram = get("telegram", "tg")
    if telegram and not telegram.startswith("@") and " " not in telegram:
        telegram = "@" + telegram
    if not (name or phone or email or telegram):
        _log(False, "empty")
        return JsonResponse({"error": "empty lead"}, status=400)

    try:
        register(name=name, phone=phone, email=email, telegram=telegram, city=get("city"),
                 consent=get("consent", "agreement").lower() in TRUE_WORDS,
                 promo=get("promo", "promo_code", "promocode"), utm=get("utm_source"), campaign=get("utm_campaign"),
                 comment=get("comment", "message"), source="site")
    except store.Invalid:
        return JsonResponse({"error": "bad lead"}, status=400)
    # Ответ одинаковый для новой и повторной заявки: по нему нельзя узнать, есть ли человек в базе
    return JsonResponse({"ok": True})
