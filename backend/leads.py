"""Приём заявок с сайта (Tilda, Taplink, своя форма): POST /api/leads/ с токеном в заголовке X-Token или ?token=."""
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


def new_id():
    return secrets.token_urlsafe(15)


def _digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())[-10:]


def _low(value):
    return str(value or "").strip().lower().lstrip("@")


def find_duplicate(phone, email, telegram):
    """Тот же человек по телефону (последние 10 цифр), почте или Telegram. Корзина тоже учитывается."""
    for doc in Doc.objects.filter(collection="students"):
        d = doc.data
        if phone and len(_digits(phone)) >= 6 and _digits(d.get("phone")) == _digits(phone):
            return doc
        if email and _low(d.get("email")) == _low(email):
            return doc
        if telegram and _low(d.get("telegram")) == _low(telegram):
            return doc
    return None


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


LEADS_PER_MINUTE = 30
MAX_AUTO_NOTES = 200
TRUE_WORDS = ("1", "true", "yes", "on", "да", "y")


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

    utm, promo = get("utm_source"), get("promo", "promo_code", "promocode")
    moment = timezone.now()  # в том же виде, в каком время пишет страница
    now = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    try:
        with transaction.atomic():
            store.lock()  # поиск дубля и запись идут под одной блокировкой: двойная отправка формы не создаст двоих
            existing = find_duplicate(phone, email, telegram)
            if existing:
                # Повторная заявка не плодит дубль: в карточке появляется заметка
                notes = list(existing.data.get("notes") or [])
                if len(notes) < MAX_AUTO_NOTES:
                    notes.append({"id": new_id(), "text": f"Повторная заявка с сайта (utm_source={utm or '—'})",
                                  "at": now, "by": None, "kind": "step"})
                    store.system_write("students", existing.doc_id, {**existing.data, "notes": notes})
                _log(True, "duplicate", existing.doc_id)
            else:
                partner = find_partner(promo, utm)
                doc_id = new_id()
                store.system_write("students", doc_id, {
                    "name": name or phone or email or telegram, "phone": phone, "telegram": telegram, "email": email,
                    "city": get("city"), "consent": get("consent", "agreement").lower() in TRUE_WORDS,
                    "stage": "new", "stageAt": now, "createdAt": now,
                    "history": [{"from": None, "to": "new", "at": now, "by": None}],
                    "cohortId": next_cohort_id(), "mentorId": None, "price": None, "lostReason": "",
                    "partnerId": partner.doc_id if partner else None,
                    "partnerShare": (partner.data.get("share") or 0) if partner else 0,
                    "promo": promo, "utmSource": utm, "utmCampaign": get("utm_campaign"),
                    "jobCompany": "", "jobPosition": "", "jobSalary": None, "offerDate": None,
                    "comment": get("comment", "message"),
                    "next": {"text": "Связаться", "date": timezone.localdate().isoformat()},
                    "payments": [], "apps": [], "notes": [], "source": "site",
                })
                _log(True, "created", doc_id)
    except store.Invalid:
        return JsonResponse({"error": "bad lead"}, status=400)
    # Ответ одинаковый для новой и повторной заявки: по нему нельзя узнать, есть ли человек в базе
    return JsonResponse({"ok": True})
