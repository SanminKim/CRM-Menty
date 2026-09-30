"""Демо-данные для локальной разработки: python manage.py seed_demo

Создаёт пользователей admin / mentor / blogger (пароль: demo12345),
два потока, партнёра и ~40 студентов на разных этапах.
Не запускайте на боевой базе.
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.models import (
    STAGE_ORDER, Cohort, JobApplication, Note, Partner, PartnerPayout, Payment, Stage,
    StageHistory, Student,
)

FIRST = ["Анна", "Дмитрий", "Екатерина", "Иван", "Мария", "Алексей", "Ольга", "Сергей", "Татьяна",
         "Никита", "Юлия", "Павел", "Ксения", "Артём", "Наталья", "Максим", "Елена", "Роман"]
LAST = ["Смирнов", "Кузнецов", "Попов", "Васильев", "Петров", "Соколов", "Михайлов", "Новиков",
        "Фёдоров", "Морозов", "Волков", "Алексеев", "Лебедев", "Семёнов", "Егоров", "Павлов"]
COMPANIES = ["Первый Бит", "Рарус", "ИнфоСофт", "АСП", "Компания «Сфера»", "Софт-Сервис", "Интерсофт"]
CITIES = ["Москва", "Казань", "Новосибирск", "Алматы", "Астана", "Екатеринбург", "Минск"]


def female(first):
    return first in {"Анна", "Екатерина", "Мария", "Ольга", "Татьяна", "Юлия", "Ксения", "Наталья", "Елена"}


class Command(BaseCommand):
    help = "Заполнить базу демо-данными"

    def add_arguments(self, parser):
        parser.add_argument("--students", type=int, default=40)

    def handle(self, *args, **opts):
        random.seed(42)
        User = get_user_model()
        admins, _ = Group.objects.get_or_create(name="Администраторы")
        mentors, _ = Group.objects.get_or_create(name="Менторы")

        def make_user(username, first, last, group=None, superuser=False):
            u, created = User.objects.get_or_create(
                username=username, defaults={"first_name": first, "last_name": last}
            )
            if created:
                u.set_password("demo12345")
                u.is_staff = superuser
                u.is_superuser = superuser
                u.save()
            if group:
                u.groups.add(group)
            return u

        admin = make_user("admin", "Админ", "Школы", admins, superuser=True)
        mentor = make_user("mentor", "Ирина", "Ментор", mentors)
        blogger_user = make_user("blogger", "Блогер", "Партнёр")

        partner, _ = Partner.objects.get_or_create(
            name="Блог «1С без боли»",
            defaults={"promo_code": "BLOG1C", "utm_source": "youtube", "share_percent": Decimal("40"),
                      "user": blogger_user},
        )
        other, _ = Partner.objects.get_or_create(
            name="Telegram-канал «Вкат в IT»",
            defaults={"promo_code": "VKAT", "utm_source": "telegram", "share_percent": Decimal("25")},
        )

        today = timezone.localdate()
        c1, _ = Cohort.objects.get_or_create(
            name="Поток 1", defaults={"start_date": today - timedelta(days=150),
                                      "end_date": today - timedelta(days=60), "price": Decimal("90000"),
                                      "is_active": False})
        c2, _ = Cohort.objects.get_or_create(
            name="Поток 2", defaults={"start_date": today - timedelta(days=45),
                                      "end_date": today + timedelta(days=45), "price": Decimal("100000")})
        c3, _ = Cohort.objects.get_or_create(
            name="Поток 3", defaults={"start_date": today + timedelta(days=30), "price": Decimal("110000")})

        now = timezone.now()
        for i in range(opts["students"]):
            first, last = random.choice(FIRST), random.choice(LAST)
            if female(first):
                last += "а"
            stage = random.choices(
                STAGE_ORDER,
                weights=[6, 4, 3, 3, 7, 3, 5, 3, 3, 4],
            )[0]
            idx = STAGE_ORDER.index(stage)
            if stage == Stage.LOST:
                idx = random.randint(0, 5)  # на каком этапе отвалился
            cohort = c1 if idx >= 6 else (c2 if idx >= 4 else c3)
            src = random.choices([partner, other, None], weights=[6, 3, 1])[0]
            created = now - timedelta(days=random.randint(5, 170) if idx >= 4 else random.randint(0, 25))

            s = Student(
                full_name=f"{last} {first}",
                phone=f"+7 9{random.randint(10, 99)} {random.randint(100, 999)}-{random.randint(10, 99)}-{random.randint(10, 99)}",
                telegram=f"@user{1000 + i}", city=random.choice(CITIES), consent_pd=True,
                partner=src, promo_code=src.promo_code if src else "",
                utm_source=src.utm_source if src else "organic",
                cohort=cohort, mentor=mentor if random.random() < 0.6 else admin,
                price=cohort.price if idx >= 3 else None,
                created_at=created,
            )
            s.stage = Stage.NEW
            s.save()
            StageHistory.objects.filter(student=s).update(changed_at=created)

            # Проигрываем путь по этапам, чтобы была история
            t = created
            for st in STAGE_ORDER[1: idx + 1]:
                t += timedelta(days=random.randint(1, 12))
                if t > now:
                    t = now - timedelta(days=random.randint(0, 3))
                s.stage = st
                s.save(changed_by=mentor)
                s.history.filter(to_stage=st).update(changed_at=t)
            if stage == Stage.LOST:
                s.stage = Stage.LOST
                s.lost_reason = random.choice(["Дорого", "Не тянет по времени", "Передумал", "Не выходит на связь"])
                s.save(changed_by=mentor)
                t = min(t + timedelta(days=3), now)
                s.history.filter(to_stage=Stage.LOST).update(changed_at=t)
            Student.objects.filter(pk=s.pk).update(stage_changed_at=t)

            # Платежи: рассрочка на 3 месяца
            if idx >= 4 and s.price:
                start = timezone.localdate(created) + timedelta(days=5)
                part = (s.price / 3).quantize(Decimal("1"))
                for n in range(3):
                    due = start + timedelta(days=30 * n)
                    paid = due <= today and random.random() < 0.85
                    Payment.objects.create(
                        student=s, amount=part if n < 2 else s.price - part * 2, due_date=due,
                        status=Payment.Status.PAID if paid else Payment.Status.PLANNED,
                        paid_date=due if paid else None, comment=f"Рассрочка {n + 1}/3",
                    )

            # Отклики для тех, кто в поиске и дальше
            if STAGE_ORDER.index(stage) >= STAGE_ORDER.index(Stage.JOB_SEARCH) and stage != Stage.LOST:
                for _ in range(random.randint(2, 6)):
                    JobApplication.objects.create(
                        student=s, company=random.choice(COMPANIES), position="Аналитик 1С",
                        status=random.choice(["applied", "hr", "test", "interview", "rejected"]),
                        applied_date=today - timedelta(days=random.randint(1, 40)),
                        interview_at=(now + timedelta(days=random.randint(0, 6), hours=random.randint(1, 8)))
                        if random.random() < 0.2 else None,
                    )
                if stage in (Stage.OFFER, Stage.PROBATION_PASSED):
                    Student.objects.filter(pk=s.pk).update(
                        job_company=random.choice(COMPANIES), job_position="Аналитик 1С",
                        job_salary=Decimal(random.choice([70000, 80000, 90000, 100000, 120000])),
                        offer_date=min(today, timezone.localdate(t)),
                    )

            if random.random() < 0.5:
                Note.objects.create(student=s, author=mentor, text=random.choice([
                    "Созвонились, мотивация высокая, хочет перейти из бухгалтерии.",
                    "Сдал ДЗ по модулю 3, есть замечания по описанию бизнес-процесса.",
                    "Пропустил два занятия — написать в понедельник.",
                    "Резюме отправлено на ревью.",
                ]))

        PartnerPayout.objects.get_or_create(
            partner=partner, amount=Decimal("50000"), defaults={"comment": "Выплата за поток 1"}
        )
        self.stdout.write(self.style.SUCCESS(
            "Готово. Вход: admin / mentor / blogger, пароль demo12345"
        ))
