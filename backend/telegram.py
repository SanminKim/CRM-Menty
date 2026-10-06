"""Telegram-бот школы.

Для людей с видео блогера бот принимает заявку: спрашивает согласие, имя и пару слов о цели — и заявка
появляется в CRM с привязкой к партнёру (промокод зашит в ссылку t.me/<бот>?start=<промокод>).
Сотрудникам бот присылает уведомления: о новой заявке — администраторам, о назначенном студенте — ментору.

Telegram сам обращается к серверу (webhook), отдельный процесс не нужен. Бот ничего не меняет в CRM,
кроме заявки и заметок человека, который ему пишет.
"""
import hashlib
import hmac
import http.client
import json
import logging
import re
import secrets
import urllib.error
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import leads, store
from .models import Account, Doc, State, TgChat

log = logging.getLogger(__name__)

PROMO_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MESSAGES_PER_MINUTE = 20
LEADS_PER_MINUTE = 20     # заявок через бота со всех чатов вместе
LEADS_PER_DAY = 300
LINK_MINUTES = 15
STUDENT_LINK_DAYS = 7     # ссылку студенту отправляют лично, открыть её он может не сразу
NOTE_NOTICE_MINUTES = 10  # о сообщениях одного человека сотруднику сообщаем не чаще
PAUSE_SECONDS = 60        # сколько не обращаться к Telegram после сбоя, чтобы не занимать сервер ожиданием
MAX_UPDATE_BYTES = 64 * 1024

HELLO = ("Здравствуйте! Здесь можно оставить заявку на обучение профессии аналитика 1С с ментором.\n\n"
         "Я задам два коротких вопроса и передам заявку ментору — он напишет вам лично.\n\n"
         "Нажимая «Продолжить», вы соглашаетесь на обработку ваших данных (имя и контакт) для связи с вами.")
ASK_NAME = "Как к вам обращаться? Напишите имя и фамилию."
ASK_CONTACT = ("В вашем профиле Telegram нет имени пользователя, поэтому ментор не сможет написать вам первым.\n"
               "Оставьте телефон: нажмите кнопку ниже или напишите номер сообщением.")
ASK_GOAL = "Расскажите в двух словах: есть ли опыт в 1С или IT и чего хотите добиться?"
THANKS = "Спасибо! Заявка у ментора, он напишет вам в ближайшее время."
ALREADY = "Ваша заявка уже у нас. Если хотите что-то добавить, напишите сюда — я передам ментору."
RECEIVED = "Ваша заявка уже у ментора, он свяжется с вами."
BUSY = "Сейчас очень много заявок. Напишите, пожалуйста, чуть позже."
PASSED = "Передал ментору."
PRESS_BUTTON = "Чтобы продолжить, нажмите кнопку «Продолжить» под сообщением выше."
TOO_SHORT = "Напишите, пожалуйста, имя текстом."
STAFF_LINKED = "Готово. Уведомления CRM будут приходить в этот чат. Отключить их можно в CRM: Настройки → Учётная запись."
STAFF_HELLO = "Этот чат подключён к CRM: сюда приходят уведомления. Заявки оставляют люди по ссылке от партнёра."
STUDENT_LINKED = ("Готово, чат подключён. В день платежа я пришлю напоминание.\n"
                  "Если хотите что-то передать ментору, напишите сюда.")
STUDENT_UNLINKED = "Напоминания о платежах теперь приходят в другой чат. Если это ошибка, напишите ментору."
STUDENT_LINK_EXPIRED = "Ссылка для подключения устарела. Попросите у ментора новую."
LINK_EXPIRED = "Ссылка для подключения устарела. Откройте CRM: Настройки → Учётная запись → «Подключить Telegram»."


def enabled():
    return bool(settings.TELEGRAM_BOT_TOKEN)


def webhook_secret():
    """Секрет, по которому сервер узнаёт запросы Telegram. Выводится из SECRET_KEY: отдельной настройки не нужно."""
    return hmac.new(settings.SECRET_KEY.encode(), b"telegram-webhook", hashlib.sha256).hexdigest()[:48]


def call(method, **params):
    """Запрос к Telegram. Возвращает результат или None; ошибки не пробрасываются, чтобы не ломать работу CRM.

    После сбоя запросы минуту не отправляются: если Telegram недоступен, сервер не тратит время на ожидание.
    """
    if not enabled() or cache.get("tg-down"):
        return None
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}",
        data=json.dumps(params).encode(), headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Telegram ответил отказом (например, человек заблокировал бота): это не сбой связи
        log.warning("Telegram %s отклонён: %s", method, exc.code)
        return None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        # В сообщение об ошибке может попасть адрес запроса с токеном, поэтому пишем только её вид
        log.warning("Telegram %s не выполнен: %s", method, type(exc).__name__)
        cache.set("tg-down", 1, PAUSE_SECONDS)
        return None
    return payload.get("result") if isinstance(payload, dict) and payload.get("ok") else None


def send(chat_id, text, **extra):
    return call("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview=True, **extra)


def send_document(chat_id, filename, content, caption=""):
    """Отправляет файл в чат. Как и call, не пробрасывает ошибки и минуту молчит после сбоя связи."""
    if not enabled() or cache.get("tg-down"):
        return None
    boundary = secrets.token_hex(16)
    parts = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                     for name, value in (("chat_id", chat_id), ("caption", caption[:1000])))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:80] or "file"
    body = (parts + f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{safe}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendDocument",
        data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        log.warning("Telegram sendDocument отклонён: %s", exc.code)
        return None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        log.warning("Telegram sendDocument не выполнен: %s", type(exc).__name__)
        cache.set("tg-down", 1, PAUSE_SECONDS)
        return None
    return payload.get("result") if isinstance(payload, dict) and payload.get("ok") else None


def bot_username():
    return State.objects.filter(pk=1).values_list("bot_username", flat=True).first() or ""


# ---------- Уведомления сотрудникам ----------

def _staff_chats(**filters):
    return TgChat.objects.filter(user__is_active=True, **filters).exclude(user__account__role=Account.Role.PARTNER)


def notify_admins(text):
    if not enabled():
        return
    for chat in _staff_chats(user__account__role=Account.Role.ADMIN):
        send(chat.chat_id, f"{text}\n{settings.PUBLIC_URL}/")


def notify_team(team_id, text, skip_user=None):
    """Сообщение сотруднику, связанному с записью команды (например, ментору о назначенном студенте)."""
    if not enabled() or not team_id:
        return
    for chat in _staff_chats(user__account__link_id=team_id):
        if skip_user is None or chat.user_id != skip_user.pk:
            send(chat.chat_id, f"{text}\n{settings.PUBLIC_URL}/")


def link_url(user):
    """Одноразовая ссылка, по которой сотрудник подключает свой Telegram к учётной записи."""
    bot = bot_username()
    if not enabled() or not bot:
        return None
    code = secrets.token_urlsafe(18)
    cache.set(f"tglink:{code}", user.pk, LINK_MINUTES * 60)
    return f"https://t.me/{bot}?start=link_{code}"


def student_link_url(student_id):
    """Одноразовая ссылка, по которой студент подключает свой чат к карточке: по ней бот напоминает о платежах."""
    bot = bot_username()
    if not enabled() or not bot:
        return None
    # Пока ссылкой не воспользовались, для студента выдаётся одна и та же: повторные нажатия не плодят записи в кэше
    code = cache.get(f"tgstu-of:{student_id}")
    if not code or cache.get(f"tgstu:{code}") != student_id:
        code = secrets.token_urlsafe(18)
        cache.set(f"tgstu-of:{student_id}", code, STUDENT_LINK_DAYS * 86400)
    cache.set(f"tgstu:{code}", student_id, STUDENT_LINK_DAYS * 86400)
    return f"https://t.me/{bot}?start=stu_{code}"


def unlink(user):
    TgChat.objects.filter(user=user).update(user=None)


def forget_student(student_id):
    """Студента удалили насовсем: сведения о его чате с ботом тоже не храним."""
    TgChat.objects.filter(student_id=student_id, user__isnull=True).delete()


def _keep(record, *fields):
    """Сохраняет чат, не трогая связь с сотрудником: её меняют только подключение и отключение уведомлений."""
    record.save(update_fields=["username", "first_name", "state", "data", "student_id", "updated_at", *fields])


def _lead_limit():
    for key, limit, seconds in (("tg-leads-minute", LEADS_PER_MINUTE, 60), ("tg-leads-day", LEADS_PER_DAY, 86400)):
        count = (cache.get(key) or 0) + 1
        cache.set(key, count, seconds)
        if count > limit:
            return True
    return False


# ---------- Приём обновлений ----------

@csrf_exempt
@require_POST
def webhook(request):
    if not enabled():
        return HttpResponseNotFound()
    given = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(given.encode(), webhook_secret().encode()):
        return HttpResponse(status=403)
    if len(request.body) > MAX_UPDATE_BYTES:
        return JsonResponse({"ok": True})
    try:
        update = json.loads(request.body)
    except (ValueError, RecursionError):
        return JsonResponse({"ok": True})
    try:
        if isinstance(update, dict):
            handle(update)
    except store.Invalid:
        pass  # текст, который нельзя сохранить: молча пропускаем
    except Exception:  # Telegram повторяет запрос при ошибке, поэтому отвечаем 200 и разбираемся по журналу
        log.exception("Сбой при обработке сообщения Telegram")
    return JsonResponse({"ok": True})


def _too_many(chat_id):
    key = f"tg:{chat_id}"
    count = (cache.get(key) or 0) + 1
    cache.set(key, count, 60)
    return count > MESSAGES_PER_MINUTE


def _clean(value, limit):
    return str(value or "").replace("\x00", "").strip()[:limit] if isinstance(value, str) else ""


def handle(update):
    query = update.get("callback_query")
    if isinstance(query, dict):
        return _on_button(query)
    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat, sender = message.get("chat") or {}, message.get("from") or {}
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return
    chat_id, sender_id = chat.get("id"), sender.get("id")
    if (chat.get("type") != "private" or not isinstance(chat_id, int) or isinstance(chat_id, bool)
            or not isinstance(sender_id, int) or isinstance(sender_id, bool) or sender.get("is_bot")):
        return  # бот работает только в личных сообщениях
    if _too_many(chat_id):
        return
    record, _ = TgChat.objects.get_or_create(chat_id=chat_id)
    record.username = _clean(sender.get("username"), 64)
    record.first_name = _clean(sender.get("first_name"), 100)
    text = _clean(message.get("text"), 1500)
    contact = message.get("contact") if isinstance(message.get("contact"), dict) else None

    command = text.split()[0].split("@")[0] if text.startswith("/") else ""
    if command == "/start":
        return _on_start(record, text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else "")
    if record.user_id:
        _keep(record)
        return send(chat_id, STAFF_HELLO)
    if record.state == "consent":
        _keep(record)
        return send(chat_id, PRESS_BUTTON)
    if record.state == "name":
        if len(text) < 2 or text.startswith("/"):
            _keep(record)
            return send(chat_id, TOO_SHORT)
        record.data = {**record.data, "name": text[:100]}
        return _ask_contact_or_goal(record)
    if record.state == "contact":
        # Номер из кнопки принимаем только свой: чужой контакт переслать нельзя
        phone = ""
        if contact and contact.get("user_id") == sender_id:
            phone = _clean(str(contact.get("phone_number") or ""), 30)
        phone = phone or text[:60]
        if len(leads._digits(phone)) < 6:
            _keep(record)
            return send(chat_id, ASK_CONTACT)
        record.data = {**record.data, "phone": phone}
        record.state = "goal"
        _keep(record)
        return send(chat_id, ASK_GOAL, reply_markup={"remove_keyboard": True})
    if record.state == "goal":
        if not text:
            _keep(record)
            return send(chat_id, ASK_GOAL)
        return _finish(record, text[:1000])
    if record.state == "done":
        _keep(record)
        if not text:
            return
        # Заметки пишем только в карточку, которая точно принадлежит этому чату
        if record.student_id and _add_note(record, text[:1000]):
            _tell_staff_about_message(record)
            return send(chat_id, PASSED)
        return send(chat_id, RECEIVED)
    return _on_start(record, "")


def _on_start(record, payload):
    if payload.startswith("link_"):
        code = payload[5:][:64]
        key = f"tglink:{code}"
        user_pk = cache.get(key)
        account = Account.objects.select_related("user").filter(user_id=user_pk).first() if user_pk else None
        # Код одноразовый: вторым его не предъявит даже тот, кто успел одновременно (add срабатывает один раз)
        if (not account or not account.user.is_active or account.role == Account.Role.PARTNER
                or not cache.add(f"tglink-used:{code}", 1, LINK_MINUTES * 60)):
            _keep(record)
            return send(record.chat_id, LINK_EXPIRED)
        cache.delete(key)
        TgChat.objects.filter(user=account.user).exclude(pk=record.pk).update(user=None)  # один чат на сотрудника
        record.user, record.state = account.user, ""
        _keep(record, "user")
        return send(record.chat_id, STAFF_LINKED)
    if record.user_id:
        _keep(record)
        return send(record.chat_id, STAFF_HELLO)  # чат сотрудника к карточке студента не привязывается
    if payload.startswith("stu_"):
        return _link_student(record, payload[4:][:64])
    if record.state == "done":
        _keep(record)
        return send(record.chat_id, ALREADY if record.student_id else RECEIVED)
    record.state = "consent"
    record.data = {"promo": payload if PROMO_RE.match(payload) else ""}
    _keep(record)
    return send(record.chat_id, HELLO, reply_markup={"inline_keyboard": [[{"text": "Продолжить", "callback_data": "consent"}]]})


def _link_student(record, code):
    """Привязывает чат к карточке по ссылке от ментора. Ссылка одноразовая: кто первым открыл, тот и подключён,
    поэтому ментор отправляет её студенту лично."""
    key, used = f"tgstu:{code}", f"tgstu-used:{code}"
    student_id = cache.get(key)
    linked, dropped = False, []
    if isinstance(student_id, str) and cache.add(used, 1, STUDENT_LINK_DAYS * 86400):
        try:
            with transaction.atomic():
                store.lock()
                doc = Doc.objects.filter(collection="students", doc_id=student_id).first()
                if doc is not None and not doc.data.get("deletedAt"):
                    # Этот чат мог быть привязан к другой карточке: там отметка о чате снимается, иначе CRM обещала бы напоминания
                    for other in Doc.objects.filter(collection="students", data__tgId=record.chat_id).exclude(doc_id=student_id):
                        store.system_write("students", other.doc_id, {k: v for k, v in other.data.items() if k != "tgId"})
                    data = {**doc.data, "tgId": record.chat_id}
                    if not data.get("telegram") and record.username:
                        data["telegram"] = f"@{record.username}"
                    store.system_write("students", student_id, data)
                    old = TgChat.objects.filter(student_id=student_id).exclude(pk=record.pk)  # один чат на студента
                    dropped = list(old.values_list("chat_id", flat=True))
                    old.update(student_id="")
                    record.state, record.student_id, record.data = "done", student_id, {}
                    linked = True
        except Exception:
            cache.delete(used)  # запись не удалась: ссылка остаётся рабочей
            raise
        cache.delete(key)
        cache.delete(f"tgstu-of:{student_id}")
    _keep(record)
    for chat_id in dropped:
        send(chat_id, STUDENT_UNLINKED)
    return send(record.chat_id, STUDENT_LINKED if linked else STUDENT_LINK_EXPIRED)


def _tell_staff_about_message(record):
    """Сотруднику: человек написал в бот. Сам текст лежит в карточке и в Telegram не дублируется."""
    if not cache.add(f"tgnote:{record.chat_id}", 1, NOTE_NOTICE_MINUTES * 60):
        return
    doc = Doc.objects.filter(collection="students", doc_id=record.student_id).first()
    if doc is None:
        return
    text = f"Сообщение в боте от {leads.plain(doc.data.get('name'))}. Текст — в карточке, раздел «Заметки»."
    mentor = doc.data.get("mentorId")
    if isinstance(mentor, str) and mentor and _staff_chats(user__account__link_id=mentor).exists():
        notify_team(mentor, text)
    else:
        notify_admins(text)


def _on_button(query):
    sender, message = query.get("from") or {}, query.get("message") or {}
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    def ack():  # убирает «часики» с кнопки; делается после основной работы и не обязателен
        if isinstance(query.get("id"), str):
            call("answerCallbackQuery", callback_query_id=query["id"])

    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat.get("type") != "private" or _too_many(chat_id):
        return ack()
    record = TgChat.objects.filter(chat_id=chat_id).first()
    if not record or record.state != "consent" or query.get("data") != "consent":
        return ack()
    if isinstance(sender, dict):
        record.username = _clean(sender.get("username"), 64)
        record.first_name = _clean(sender.get("first_name"), 100)
    record.state = "name"
    record.data = {**record.data, "consent": True}
    _keep(record)
    send(chat_id, ASK_NAME)
    ack()


def _ask_contact_or_goal(record):
    if record.username:
        record.state = "goal"
        _keep(record)
        return send(record.chat_id, ASK_GOAL)
    record.state = "contact"
    _keep(record)
    keyboard = {"keyboard": [[{"text": "Отправить мой номер", "request_contact": True}]],
                "resize_keyboard": True, "one_time_keyboard": True}
    return send(record.chat_id, ASK_CONTACT, reply_markup=keyboard)


def _finish(record, goal):
    data = record.data
    if _lead_limit():
        _keep(record)
        return send(record.chat_id, BUSY)
    student_id, created, matched = leads.register(
        name=data.get("name") or record.first_name, telegram=f"@{record.username}" if record.username else "",
        phone=data.get("phone") or "", consent=bool(data.get("consent")), promo=data.get("promo") or "",
        comment=goal, source="telegram", tg_id=record.chat_id,
    )
    # К чужой карточке чат не привязываем: совпадение по телефону или имени пользователя ничего не доказывает,
    # иначе посторонний, зная чужой номер, мог бы писать заметки в карточку этого человека
    own = created or matched == "tg"
    record.state, record.student_id, record.data = "done", student_id if own else "", {}
    _keep(record)
    send(record.chat_id, THANKS, reply_markup={"remove_keyboard": True})


def _add_note(record, text):
    """Сообщение после заявки попадает заметкой в карточку. Возвращает False, если карточки уже нет."""
    with transaction.atomic():
        store.lock()
        doc = Doc.objects.filter(collection="students", doc_id=record.student_id).first()
        if doc is None:
            return False
        notes = list(doc.data.get("notes") or [])
        if len(notes) >= leads.MAX_AUTO_NOTES:
            return False
        notes.append({"id": leads.new_id(), "text": f"Сообщение в боте: {text}", "at": leads.stamp(), "by": None, "kind": "step"})
        store.system_write("students", doc.doc_id, {**doc.data, "notes": notes})
    return True
