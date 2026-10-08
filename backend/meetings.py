"""Встречи глазами студента: кому бот напоминает, что он пишет и как студент отвечает «буду / не буду».

Утром в день встречи и за несколько минут до начала бот пишет студентам встречи со ссылкой и кнопками
«Буду» / «Не буду». Ответ записывается во встречу (`rsvp`: id студента → {"a": "yes" | "no", "at": время}),
его видно в карточке дня и в календаре. Настройки — в `config/main` → `meetRemind`, у встречи можно выключить
напоминание (`remind: false`). Кто участник, страница считает так же (`meetPeople` в web/index.html).
"""
import datetime
import re

from django.db import transaction
from django.utils import timezone

from . import leads, reports, store
from .models import Doc

STUDY = ("studying", "final_project")   # о занятиях потока напоминаем тем, кто учится
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


def participants(meeting, students):
    """Студенты встречи: тот, с кем она назначена, и учащиеся потока, если это занятие группы. students — {id: данные}."""
    out = []
    sid, cohort = meeting.get("studentId"), meeting.get("cohortId")
    for student_id, s in students.items():
        if not isinstance(s, dict) or s.get("deletedAt") or s.get("stage") == "lost":
            continue
        personal = isinstance(sid, str) and sid == student_id
        group = isinstance(cohort, str) and cohort and s.get("cohortId") == cohort and s.get("stage") in STUDY
        if personal or group:
            out.append(student_id)
    return sorted(out)


def answer_of(meeting, student_id):
    rsvp = meeting.get("rsvp")
    item = rsvp.get(student_id) if isinstance(rsvp, dict) else None
    value = item.get("a") if isinstance(item, dict) else None
    return value if value in ("yes", "no") else None


def link_of(meeting, cohorts):
    """Ссылка на созвон: у самой встречи или, если её нет, постоянная ссылка потока."""
    for value in (meeting.get("link"), (cohorts.get(meeting.get("cohortId")) or {}).get("link") if isinstance(meeting.get("cohortId"), str) else None):
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
