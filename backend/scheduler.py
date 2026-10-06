"""Дела по расписанию: утренняя сводка, напоминание о встрече и копия базы администратору в Telegram.

`run()` вызывается раз в минуту (команда `tick`). Каждое дело отмечается в кэше, поэтому повторный вызов
ничего не отправляет второй раз. Всё уходит только сотрудникам, подключившим Telegram; партнёрам — ничего.
В сообщениях только числа, названия встреч и время: ни телефонов, ни почты.
"""
import datetime
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import backup, leads, reports, telegram
from .models import Account, Doc, TgChat

log = logging.getLogger(__name__)

DIGEST_HOURS = range(9, 12)   # сводка приходит утром; если сервер в это время не работал, днём её уже не шлём
BACKUP_FROM_HOUR = 4
PAY_HOURS = range(10, 21)     # студентам пишем днём
REMIND_MINUTES = 60
RETRY_MINUTES = 60
KEEP_SECONDS = 3 * 86400      # сколько помнить, что дело за этот день сделано
CLOSED = ("lost", "probation_passed")
PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{5,}\d")


def run(now=None):
    if not telegram.enabled():
        return
    now = timezone.localtime(now) if now else timezone.localtime()
    for job in (_reminders, _digests, _pay_reminders, _backup):
        try:
            job(now)
        except Exception:  # одно сорвавшееся дело не должно останавливать остальные и сам планировщик
            log.exception("Планировщик: %s не выполнено", job.__name__)


def _once(key):
    """True, если дело с таким ключом ещё не делали. Планировщик один, поэтому гонки здесь нет."""
    if cache.get(key):
        return False
    cache.set(key, 1, KEEP_SECONDS)
    return True


def _docs(collection):
    return [(d.doc_id, d.data) for d in Doc.objects.filter(collection=collection).order_by("doc_id") if isinstance(d.data, dict)]


def _money(value):
    return f"{reports._round(value):,}".replace(",", " ") + " ₽"


def _staff():
    """Сотрудники с подключённым Telegram: [(чат, роль, id записи в команде)]."""
    chats = telegram._staff_chats().select_related("user__account")
    return [(c.chat_id, c.user.account.role, c.user.account.link_id) for c in chats if getattr(c.user, "account", None)]


# ---------- Утренняя сводка ----------

def _digests(now):
    if now.hour not in DIGEST_HOURS:
        return
    today = now.date().isoformat()
    staff = _staff()
    if not staff:
        return
    students = [(i, s) for i, s in _docs("students") if not s.get("deletedAt")]
    meetings = [m for _, m in _docs("meetings")]
    team = {i for i, _ in _docs("team")}
    for chat_id, role, link_id in staff:
        if not _once(f"sched:digest:{today}:{chat_id}"):
            continue
        try:
            text = _digest(today, role == Account.Role.ADMIN, link_id, students, meetings, team)
        except Exception:  # кривая запись не должна оставить без сводки остальных
            log.exception("Планировщик: сводка не собрана")
            continue
        if text:
            telegram.send(chat_id, f"{text}\n{settings.PUBLIC_URL}/")


def _digest(today, is_admin, link_id, students, meetings, team):
    mine = [(i, s) for i, s in students if is_admin or (link_id and s.get("mentorId") == link_id)]
    own_ids = {i for i, _ in mine}
    active = [s for _, s in mine if s.get("stage") not in CLOSED]
    steps = [s["next"]["date"] for s in active
             if isinstance(s.get("next"), dict) and s["next"].get("text") and reports._day(s["next"].get("date"))]
    due = [d for d in steps if d[:10] <= today]
    late = [d for d in due if d[:10] < today]
    unpaid = [p for _, s in mine if s.get("stage") != "lost" for p in reports._list(s.get("payments"))
              if not p.get("paid") and reports._day(p.get("due"))]
    late_pays = [p for p in unpaid if p["due"][:10] < today]
    overdue = [p for p in late_pays if reports._number(p.get("amount")) > 0]
    # Платёж от дохода без суммы: студент ещё не сообщил, сколько заработал
    no_income = [p for p in late_pays if reports._number(p.get("amount")) <= 0 and _percent(p.get("percent"))]
    pay_today = [p for p in unpaid if p["due"][:10] == today]
    meets = [m for m in meetings if reports._day(m.get("date")) == today and (m.get("status") or "planned") == "planned"
             and (is_admin or (link_id and m.get("mentorId") == link_id)
                  or (isinstance(m.get("studentId"), str) and m["studentId"] in own_ids))]
    orphans = [s for s in active if not (isinstance(s.get("mentorId"), str) and s["mentorId"] in team)] if is_admin else []
    lines = []
    if due:
        lines.append(f"• шагов: {len(due)}" + (f" (просрочено {len(late)})" if late else ""))
    if overdue:
        lines.append(f"• просрочено платежей: {len(overdue)} на {_money(reports._total(reports._number(p.get('amount')) for p in overdue))}")
    if no_income:
        lines.append(f"• ждут сведений о доходе: {len(no_income)}")
    if pay_today:
        lines.append(f"• срок оплаты сегодня: {len(pay_today)}")
    if meets:
        lines.append(f"• встреч: {len(meets)}")
    if orphans:
        lines.append(f"• заявок без ментора: {len(orphans)}")
    return "Доброе утро! На сегодня:\n" + "\n".join(lines) if lines else ""


# ---------- Напоминание о встрече ----------

def _reminders(now):
    today = now.date().isoformat()
    staff = None
    for meeting_id, m in _docs("meetings"):
        if reports._day(m.get("date")) != today or (m.get("status") or "planned") != "planned":
            continue
        clock = m.get("time")
        try:
            hour, minute = (int(x) for x in clock.split(":")) if isinstance(clock, str) else (None, None)
            start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, TypeError):
            continue  # встреча на весь день или время записано не так: напоминать не о чем
        clock = f"{hour:02d}:{minute:02d}"
        left = (start - now).total_seconds() / 60
        if not 0 < left <= REMIND_MINUTES or not _once(f"sched:meet:{meeting_id}:{today}T{clock}"):
            continue
        staff = _staff() if staff is None else staff
        mentor = m.get("mentorId") if isinstance(m.get("mentorId"), str) else ""
        to = [chat for chat, role, link in staff if mentor and link == mentor] \
            or [chat for chat, role, link in staff if role == Account.Role.ADMIN]
        # Название пишут сотрудники: ссылки убираются, а длинные числа прячутся, чтобы в Telegram не ушёл телефон
        title = PHONE_RE.sub("…", leads.plain(m.get("title") if isinstance(m.get("title"), str) else "", 80)) or "встреча"
        for chat_id in to:
            telegram.send(chat_id, f"Через {max(round(left), 1)} мин, в {clock}: {title}\n{settings.PUBLIC_URL}/")


# ---------- Напоминание студенту о платеже ----------

PAY_TEXT = "сегодня срок платежа за обучение — {amount}."
ASK_TEXT = ("сегодня день расчёта за обучение: {percent}% от вашего дохода за прошедший месяц. "
            "Напишите, пожалуйста, сколько вы заработали, и пришлите сюда фото начисления зарплаты и отработанных часов.")
SLIP_TEXT = "Пришлите, пожалуйста, сюда фото или документ с начислением зарплаты за месяц."
PAID_TEXT = "Если уже оплатили, напишите об этом сюда — я передам ментору."


def _percent(value):
    """Процент с дохода для платежа без суммы (почасовая оплата) или 0."""
    number = reports._number(value)
    return number if 0 < number <= 100 else 0


def _pay_reminders(now):
    """В день платежа бот пишет студенту, чей чат подключён к карточке. Одно сообщение в день.

    Если сумма платежа известна, бот называет её. Если платёж считается от дохода (в нём записан только процент),
    бот просит сообщить доход: сумму потом вносит ментор.
    """
    if now.hour not in PAY_HOURS:
        return
    today = now.date().isoformat()
    # Пишем только в чат, который сам привязан к этой карточке: одного tgId в записи студента недостаточно
    bound = {c.chat_id: c.student_id for c in TgChat.objects.filter(user__isnull=True).exclude(student_id="")}
    for student_id, s in _docs("students"):
        chat_id = s.get("tgId")
        if s.get("deletedAt") or s.get("stage") == "lost" or isinstance(chat_id, bool) or not isinstance(chat_id, int) \
                or bound.get(chat_id) != student_id:
            continue
        due = [p for p in reports._list(s.get("payments")) if not p.get("paid") and reports._day(p.get("due")) == today]
        total = reports._total(a for a in (reports._number(p.get("amount")) for p in due) if a > 0)
        rates = [_percent(p.get("percent")) for p in due if reports._number(p.get("amount")) <= 0]
        rates = sorted({r for r in rates if r})
        parts = ([PAY_TEXT.format(amount=_money(total))] if total > 0 else []) \
            + ([ASK_TEXT.format(percent=" и ".join(f"{float(r):g}".replace(".", ",") for r in rates))] if rates else [])
        if not parts:
            continue
        # Платёж с оклада: просим подтверждение начисления. При почасовой оплате фото уже запрошено в вопросе о доходе
        salary = any(p.get("salary") is True or str(p.get("comment") or "").startswith("С зарплаты") for p in due)
        text = "Здравствуйте! Напоминаю: " + " Также ".join(parts) + (" " + SLIP_TEXT if salary and not rates else "") \
            + ("\n" + PAID_TEXT if total > 0 else "")
        key = f"sched:pay:{student_id}:{today}"
        # Отметка ставится после отправки: если Telegram был недоступен, напоминание уйдёт позже в тот же день
        if not cache.get(key) and telegram.send(chat_id, text):
            cache.set(key, 1, KEEP_SECONDS)


# ---------- Копия базы вне сервера ----------

def _backup(now):
    if now.hour < BACKUP_FROM_HOUR or not backup.passphrase_ok(settings.BACKUP_PASSPHRASE):
        return
    today = now.date().isoformat()
    key = f"sched:backup:{today}"
    state = cache.get(key)
    if state == "ok":
        return
    if state:  # прошлая попытка не удалась: повторяем не чаще раза в час
        try:
            if now - datetime.datetime.fromisoformat(state) < datetime.timedelta(minutes=RETRY_MINUTES):
                return
        except (ValueError, TypeError):
            pass
    admins = [chat for chat, role, _ in _staff() if role == Account.Role.ADMIN]
    if not admins:
        return
    cache.set(key, now.isoformat(), KEEP_SECONDS)  # если сборка копии сорвётся, следующая попытка будет через час, а не через минуту
    content = backup.encrypt(backup.dump(), settings.BACKUP_PASSPHRASE)
    caption = f"Копия базы CRM за {now.strftime('%d.%m.%Y')}. Зашифрована паролем копии; загружается в CRM: Настройки → Резервная копия."
    sent = [telegram.send_document(chat, f"crm-menti-{today}.crmbackup", content, caption) for chat in admins]
    cache.set(key, "ok" if any(sent) else now.isoformat(), KEEP_SECONDS)
