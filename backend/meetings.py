"""Встречи глазами студента: кому бот напоминает, что он пишет и как студент отвечает «буду / не буду».

Утром в день встречи и за несколько минут до начала бот пишет студентам встречи со ссылкой и кнопками
«Буду» / «Не буду». Ответ записывается во встречу (`rsvp`: id студента → {"a": "yes" | "no", "at": время}),
его видно в карточке дня и в календаре. Настройки — в `config/main` → `meetRemind`, у встречи можно выключить
напоминание (`remind: false`). Кто участник, страница считает так же (`meetPeople` в web/index.html).

Участники встречи: ведущий (`mentorId`), приглашённые менторы (`mentorIds`), студенты (`studentIds`, у старых
встреч — `studentId`) и потоки (`cohortIds`, у старых — `cohortId`: на занятие потока приходят его учащиеся).
Когда встречу создают, переносят, меняют состав или ссылку, отменяют или удаляют и автор просит сообщить
участникам, бот пишет каждому, что изменилось для него (`notices`). Сообщения уходят после фиксации записи
и в фоне: сбой Telegram запись не отменяет и не задерживает ответ странице.
"""
import datetime
import logging
import re
import threading
from typing import NamedTuple

from django.db import transaction
from django.utils import timezone

from django.conf import settings
from django.core.cache import cache
from django.db import connections

from . import leads, reports, store
from .models import Doc, TgChat

log = logging.getLogger(__name__)

DEFAULTS = {
    "on": False,
    "morning": "09:00",
    "before": 15,
    "textMorning": "📅 {имя}, сегодня в {время} — {название}.\nСсылка: {ссылка}\nПридёте?",
    "textSoon": "⏰ {имя}, через {минут} мин начинаем: {название}.\nСсылка: {ссылка}",
}
CLOCK_RE = re.compile(r"(\d{2}):(\d{2})\Z")
LINK_RE = re.compile(r"https?://[^\s<>\"]{1,500}\Z")
ANSWERS = {"y": "yes", "n": "no"}
GUESTS_MAX = 300   # участников одного вида у встречи
PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{5,}\d")
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")


class Guests(NamedTuple):
    mentors: tuple
    students: tuple
    cohorts: tuple


def _ids(*values):
    """Идентификаторы из строки или списка: без мусора и повторов, в исходном порядке."""
    out = []
    for value in values:
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and store.ID_RE.fullmatch(item) and item not in out:
                out.append(item)
    return tuple(out)


def guests(meeting):
    """Кто позван на встречу: ведущий и приглашённые менторы, студенты поимённо и потоки. Кривые записи пропускаются."""
    m = meeting if isinstance(meeting, dict) else {}
    return Guests(_ids(m.get("mentorId"), m.get("mentorIds")), _ids(m.get("studentId"), m.get("studentIds")),
                  _ids(m.get("cohortId"), m.get("cohortIds")))


REPEATS = {"day": "каждый день", "week": "каждую неделю", "2weeks": "каждые две недели"}


def series_problem(meeting):
    """Причина отказа для серии повторяющихся встреч: знак серии и правило повтора {every, until}."""
    sid, rule = meeting.get("seriesId"), meeting.get("repeat")
    if sid is not None and not (isinstance(sid, str) and store.ID_RE.fullmatch(sid)):
        return "Серия встреч записана неверно"
    if rule is not None and not (isinstance(rule, dict) and set(rule) <= {"every", "until"} and isinstance(rule.get("every"), str)
                                 and rule["every"] in REPEATS and isinstance(rule.get("until"), str) and len(rule["until"]) == 10
                                 and _date({"date": rule["until"]}) is not None):
        return "Правило повтора записано неверно"
    return None


def repeat_text(meeting):
    """«каждую неделю по 4 июня» или пустая строка."""
    rule = meeting.get("repeat") if isinstance(meeting, dict) else None
    if not isinstance(rule, dict) or not isinstance(rule.get("every"), str) or rule["every"] not in REPEATS:
        return ""
    until = _date({"date": rule.get("until")})
    return REPEATS[rule["every"]] + (f" по {until.day} {MONTHS[until.month - 1]}" if until else "")


def guests_problem(meeting):
    """Причина отказа для списков участников, которые прислала страница, или None."""
    for key in ("mentorIds", "studentIds", "cohortIds"):
        value = meeting.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or len(value) > GUESTS_MAX or not all(isinstance(x, str) and store.ID_RE.fullmatch(x) for x in value):
            return "Список участников встречи записан неверно"
    return None


def settings_of(config):
    """Настройки напоминаний из config/main в проверенном виде."""
    raw = config.get("meetRemind") if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["on"] = raw.get("on") is True
    clock = raw.get("morning")
    match = CLOCK_RE.match(clock) if isinstance(clock, str) else None
    if match and int(match[1]) < 24 and int(match[2]) < 60:
        out["morning"] = clock
    before = reports._number(raw.get("before"))
    if 1 <= before <= 180:
        out["before"] = int(before)
    for key in ("textMorning", "textSoon"):
        text = raw.get(key)
        if isinstance(text, str) and text.strip():
            out[key] = text.strip()[:1000]
    return out


def load_settings():
    doc = Doc.objects.filter(collection="config", doc_id="main").first()
    return settings_of(doc.data if doc else {})


def start_of(meeting, now):
    """Начало встречи сегодня (в часовом поясе now) или None: другая дата, нет времени, время записано криво."""
    if reports._day(meeting.get("date")) != now.date().isoformat():
        return None
    clock = meeting.get("time")
    match = CLOCK_RE.match(clock) if isinstance(clock, str) else None
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        return None
    return now.replace(hour=int(match[1]), minute=int(match[2]), second=0, microsecond=0)


def participants(meeting, students, stages=None):
    """Студенты встречи: тот, с кем она назначена, и учащиеся потока, если это занятие группы. students — {id: данные}.
    Учащиеся — люди на этапах группы «Обучение» (состав этапов настраивается), закрытые этапы пропускаются."""
    stages = stages or reports.current_stages()
    out = []
    g = guests(meeting)
    for student_id, s in students.items():
        if not isinstance(s, dict) or s.get("deletedAt") or (isinstance(s.get("stage"), str) and s["stage"] in stages.closed):
            continue
        personal = student_id in g.students
        group = s.get("cohortId") in g.cohorts and isinstance(s.get("stage"), str) and s["stage"] in stages.study
        if personal or group:
            out.append(student_id)
    return sorted(out)


def answer_of(meeting, student_id):
    rsvp = meeting.get("rsvp")
    item = rsvp.get(student_id) if isinstance(rsvp, dict) else None
    value = item.get("a") if isinstance(item, dict) else None
    return value if value in ("yes", "no") else None


def link_of(meeting, cohorts):
    """Ссылка на созвон: у самой встречи или, если её нет, постоянная ссылка первого из её потоков."""
    for value in (meeting.get("link"), *((cohorts.get(c) or {}).get("link") for c in guests(meeting).cohorts)):
        if isinstance(value, str) and LINK_RE.match(value.strip()):
            return value.strip()
    return ""


def first_name(name):
    """«Иванов Пётр» → «Пётр»: так к студенту обращается и напоминание об оплате на странице."""
    parts = str(name or "").split() if isinstance(name, str) else []
    return parts[1] if len(parts) > 1 else (parts[0] if parts else "")


def render(template, **values):
    """Подставляет {имя}, {время}, {название}, {ссылка}, {минут}. Строки со ссылкой убираются, если ссылки нет."""
    lines = [line for line in template.split("\n") if values.get("ссылка") or "{ссылка}" not in line]
    text = "\n".join(lines)
    if not values.get("имя"):  # имени нет: обращение убираем вместе с запятой
        text = text.replace("{имя}, ", "").replace("{имя}", "")
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text.strip()[:3500]


def buttons(meeting_id, chosen=None):
    """Кнопки ответа. Выбранный ответ помечен галочкой; нажать можно и другой — ответ поменяется."""
    data = {k: f"r:{meeting_id}:{k}" for k in ANSWERS}
    if any(len(v.encode()) > 64 for v in data.values()):
        return None  # Telegram не примет такую кнопку: идентификатор встречи слишком длинный
    label = {"y": "Буду", "n": "Не буду"}
    mark = {"y": "✅ ", "n": "❌ "}
    return {"inline_keyboard": [[{"text": ("✓ " if ANSWERS[k] == chosen else mark[k]) + label[k], "callback_data": data[k]} for k in ("y", "n")]]}


def record_answer(student_id, meeting_id, code, now=None):
    """Записывает ответ студента. Возвращает (ответ, текст для студента); ответ None, если записать нельзя."""
    value = ANSWERS.get(code)
    if value is None or not isinstance(meeting_id, str) or not store.ID_RE.fullmatch(meeting_id):
        return None, "Эта кнопка больше не работает."
    now = timezone.localtime(now) if now else timezone.localtime()
    with transaction.atomic():
        store.lock()
        meeting = Doc.objects.filter(collection="meetings", doc_id=meeting_id).first()
        student = Doc.objects.filter(collection="students", doc_id=student_id).first()
        if meeting is None or student is None or (meeting.data.get("status") or "planned") != "planned":
            return None, "Эта встреча отменена или уже прошла."
        if student_id not in participants(meeting.data, {student_id: student.data}):
            return None, "Эта встреча вас больше не касается."
        start = start_of(meeting.data, now)
        day = reports._day(meeting.data.get("date"))
        if (start is not None and start <= now) or (start is None and day and day < now.date().isoformat()):
            return None, "Встреча уже началась — ответ не записан."
        rsvp = meeting.data.get("rsvp") if isinstance(meeting.data.get("rsvp"), dict) else {}
        rsvp = {**rsvp, student_id: {"a": value, "at": leads.stamp()}}
        store.system_write("meetings", meeting_id, {**meeting.data, "rsvp": rsvp})
    return value, "Записали: будете. До встречи!" if value == "yes" else "Записали: не придёте. Спасибо, что предупредили."


def summary(meeting, students):
    """Для напоминания ведущему: «Будут 3 из 5. Не будут: Пётр И.» или пустая строка."""
    people = participants(meeting, students)
    if not people:
        return ""
    yes = [s for s in people if answer_of(meeting, s) == "yes"]
    no = [reports.mask(students[s].get("name")) for s in people if answer_of(meeting, s) == "no"]
    return f"Будут: {len(yes)} из {len(people)}." + (f" Не будут: {', '.join(no)}." if no else "")


def minutes_between(start, now):
    return max(round((start - now) / datetime.timedelta(minutes=1)), 1)


# ---------- Сообщения участникам об изменении встречи ----------

def safe_title(meeting, limit=120):
    """Название пишут сотрудники: ссылки убираются, длинные числа прячутся, чтобы в Telegram не ушёл телефон."""
    title = meeting.get("title") if isinstance(meeting.get("title"), str) else ""
    return PHONE_RE.sub("…", leads.plain(title, limit)) or "встреча"


def _date(meeting):
    """Дата встречи или None: дата записана не так или такого дня нет (2030-02-31)."""
    day = reports._day(meeting.get("date")) if isinstance(meeting, dict) else None
    try:
        return datetime.date.fromisoformat(day) if day else None
    except ValueError:
        return None


def when(meeting):
    """«ср, 14 мая, 10:00–11:00» или «ср, 14 мая, весь день»."""
    d = _date(meeting)
    if d is None:
        return "дата не указана"
    out = f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"
    clock = meeting.get("time")
    match = CLOCK_RE.match(clock) if isinstance(clock, str) else None
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        return out + ", весь день"
    start = int(match[1]) * 60 + int(match[2])
    duration = reports._number(meeting.get("duration"))
    end = min(start + (int(duration) if 0 < duration <= 1440 else 60), 1440)
    return out + f", {start // 60:02d}:{start % 60:02d}–{end // 60 % 24:02d}:{end % 60:02d}"


def _live(meeting, today):
    """Встреча, о которой есть смысл сообщать: запланирована и не в прошлом."""
    d = _date(meeting)
    return d is not None and (meeting.get("status") or "planned") == "planned" and d.isoformat() >= today


def _people(meeting, students, stages, today):
    """{("t", id ментора) | ("s", id студента)} — кому сообщать о встрече. Студентам — только если у встречи не выключены сообщения."""
    if not _live(meeting, today):
        return set()
    out = {("t", t) for t in guests(meeting).mentors}
    if meeting.get("remind") is not False:
        out |= {("s", s) for s in participants(meeting, students, stages)}
    return out


def _changes(old, new, cohorts):
    lines = []
    if when(old) != when(new):
        lines += [f"Было: {when(old)}", f"Стало: {when(new)}"]
    if safe_title(old) != safe_title(new):
        lines.append(f"Новое название: {safe_title(new)}")
    if link_of(old, cohorts) != link_of(new, cohorts) and link_of(new, cohorts):
        lines.append(f"Новая ссылка: {link_of(new, cohorts)}")
    return lines


def notices(meeting_id, old, new, author=None, today=None, series=False):
    """Сообщения участникам об изменении встречи: [(чат, текст, доп. поля)]. Автору изменения не пишем.
    series — изменение касается этой встречи и всех следующих в серии: текст говорит обо всех сразу."""
    today = today or timezone.localdate().isoformat()
    if not _live(old, today) and not _live(new, today):
        return []   # прошедшая или отменённая встреча: сообщать не о чем, базу не читаем
    docs = Doc.objects.filter(collection__in=("students", "cohorts")).only("collection", "doc_id", "data")
    students = {d.doc_id: d.data for d in docs if d.collection == "students" and isinstance(d.data, dict)}
    cohorts = {d.doc_id: d.data for d in docs if d.collection == "cohorts" and isinstance(d.data, dict)}
    stages = reports.current_stages()
    before, after = _people(old, students, stages, today), _people(new, students, stages, today)
    if not before and not after:
        return []
    # Чаты: сотрудник — по записи в команде, студент — только чат, привязанный к его карточке
    team = {}
    for chat in TgChat.objects.filter(user__is_active=True, user__account__role__in=("admin", "mentor")).select_related("user__account"):
        if author is None or chat.user_id != author.pk:
            team.setdefault(chat.user.account.link_id, []).append(chat.chat_id)
    bound = {c.chat_id: c.student_id for c in TgChat.objects.filter(user__isnull=True).exclude(student_id="")}

    def chats(person):
        kind, pid = person
        if kind == "t":
            return team.get(pid, [])
        chat_id = (students.get(pid) or {}).get("tgId")
        return [chat_id] if isinstance(chat_id, int) and not isinstance(chat_id, bool) and bound.get(chat_id) == pid else []

    out = []
    for person in sorted(before | after):
        kind, pid = person
        target = new if person in after else old
        head = safe_title(target)
        crm = f"\n{settings.PUBLIC_URL}/#/calendar/day-{reports._day(target.get('date'))}" if kind == "t" and reports._day(target.get("date")) else ""
        link = link_of(target, cohorts)
        extra = {}
        if person in after and person not in before:
            name = first_name((students.get(pid) or {}).get("name")) if kind == "s" else ""
            rule = repeat_text(new) if series else ""
            text = f"📅 {name + ', в' if name else 'В'}ас пригласили на встречу: {head}\n{when(new)}" + (f"\nПовторяется {rule}" if rule else "") + (f"\nСсылка: {link}" if link else "")
            if kind == "s":
                markup = buttons(meeting_id, answer_of(new, pid))
                extra = {"reply_markup": markup} if markup else {}
        elif person in before and person not in after:
            if kind == "s" and new is not None and _live(new, today) and pid in participants(new, students, stages):
                continue  # студенту просто выключили сообщения: он по-прежнему участник, писать ему не о чем
            gone = new is None or (new.get("status") or "planned") != "planned" or not _live(new, today)
            if gone and new is not None and (new.get("status") or "planned") != "cancelled":
                continue  # отметили прошедшей или перенесли в прошлое: это не отмена, сообщать не о чем
            if series:
                text = (f"❌ Отменены эта и все следующие встречи: {head}\nНачиная с {when(old)}" if gone
                        else f"Вы больше не участвуете во встречах: {head}\nНачиная с {when(old)}")
            else:
                text = (f"❌ Встреча отменена: {head}\n{when(old)}" if gone
                        else f"Вы больше не участвуете во встрече: {head}\n{when(old)}")
        else:
            lines = _changes(old, new, cohorts)
            if not lines:
                continue
            verb = "перенесена" if when(old) != when(new) else "изменена"
            text = f"🔁 Встреча {verb}{' (и все следующие)' if series else ''}: {safe_title(old)}\n" + "\n".join(lines)
        for chat_id in chats(person):
            out.append((chat_id, text + crm, extra))
    return out


NOTIFY_LIMIT = 30      # изменений со «сообщить участникам» от одного человека
NOTIFY_WINDOW = 3600   # за час: больше — сообщения не уходят, запись сохраняется


def deliver(messages):
    from . import telegram  # позднее подключение: telegram пользуется этим модулем
    for chat_id, text, extra in messages:
        try:
            telegram.send(chat_id, text, **extra)
        except Exception:  # одно неудачное сообщение не мешает остальным
            log.exception("Сообщение о встрече не отправлено")


def _send_all(messages):
    try:
        deliver(messages)
    finally:
        connections.close_all()  # Telegram отмечает сбои в кэше в базе: соединение этого потока закрываем


def dispatch(messages):
    """Отправка в фоне: ответ странице не ждёт Telegram."""
    if messages:
        threading.Thread(target=_send_all, args=(messages,), daemon=True).start()


def tell(meeting_id, old, new, author, series=False):
    """Готовит сообщения сейчас (внутри записи), отправляет после её фиксации."""
    from . import telegram
    if not telegram.enabled():
        return
    try:
        messages = notices(meeting_id, old, new, author, series=series)
    except Exception:  # сбой при подготовке сообщений не должен отменять саму запись
        log.exception("Сообщения о встрече не подготовлены")
        return
    if not messages:
        return
    # Защита от рассылки по кругу: не больше NOTIFY_LIMIT изменений с сообщениями от одного человека в час
    key = f"meet-notify:{getattr(author, 'pk', '')}"
    cache.add(key, 0, NOTIFY_WINDOW)
    try:
        count = cache.incr(key)
    except ValueError:
        count = 1
    if count > NOTIFY_LIMIT:
        log.warning("Сообщения о встрече не отправлены: слишком часто")
        return
    transaction.on_commit(lambda: dispatch(messages))
