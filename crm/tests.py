from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.backup import BackupError, import_backup
from crm.models import Cohort, JobApplication, Meeting, Partner, PartnerPayout, Payment, Stage, Student
from crm.services import dashboard_metrics, funnel


class BaseCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", password="pass12345")
        self.admin.groups.add(Group.objects.get(name="Администраторы"))
        self.mentor = User.objects.create_user("mentor", password="pass12345")
        self.mentor.groups.add(Group.objects.get(name="Менторы"))
        self.other_mentor = User.objects.create_user("mentor2", password="pass12345")
        self.other_mentor.groups.add(Group.objects.get(name="Менторы"))
        self.blogger = User.objects.create_user("blogger", password="pass12345")
        self.partner = Partner.objects.create(
            name="Блогер", promo_code="BLOG", utm_source="youtube", share_percent=Decimal("40"),
            user=self.blogger,
        )


class StudentModelTests(BaseCase):
    def test_stage_history_recorded(self):
        s = Student.objects.create(full_name="Иванов Иван")
        s.stage = Stage.CONTACTED
        s.save(changed_by=self.mentor)
        self.assertEqual(s.history.count(), 2)
        last = s.history.first()
        self.assertEqual((last.from_stage, last.to_stage), (Stage.NEW, Stage.CONTACTED))
        self.assertEqual(last.changed_by, self.mentor)

    def test_partner_share_is_fixed_at_binding(self):
        s = Student.objects.create(full_name="А", partner=self.partner)
        self.assertEqual(s.partner_share_percent, Decimal("40"))
        self.partner.share_percent = Decimal("10")
        self.partner.save()
        s.refresh_from_db()
        s.save()
        self.assertEqual(s.partner_share_percent, Decimal("40"))

    def test_debt_and_overdue(self):
        s = Student.objects.create(full_name="А", price=Decimal("90000"))
        Payment.objects.create(student=s, amount=Decimal("30000"), due_date=timezone.localdate(),
                               status=Payment.Status.PAID)
        Payment.objects.create(student=s, amount=Decimal("30000"),
                               due_date=timezone.localdate() - timedelta(days=3))
        self.assertEqual(s.debt, Decimal("60000"))
        self.assertTrue(s.has_overdue)

    def test_accepted_offer_moves_student(self):
        s = Student.objects.create(full_name="А", stage=Stage.JOB_SEARCH)
        JobApplication.objects.create(student=s, company="Рарус", status="accepted", salary=Decimal("90000"))
        s.refresh_from_db()
        self.assertEqual(s.stage, Stage.OFFER)
        self.assertEqual(s.job_company, "Рарус")
        self.assertEqual(s.job_salary, Decimal("90000"))

    def test_partner_accrual_and_balance(self):
        s = Student.objects.create(full_name="А", partner=self.partner)
        Payment.objects.create(student=s, amount=Decimal("100000"), due_date=timezone.localdate(),
                               status=Payment.Status.PAID)
        Payment.objects.create(student=s, amount=Decimal("50000"), due_date=timezone.localdate())
        PartnerPayout.objects.create(partner=self.partner, amount=Decimal("15000"))
        self.assertEqual(self.partner.accrued(), Decimal("40000.00"))
        self.assertEqual(self.partner.balance(), Decimal("25000.00"))


class MetricsTests(BaseCase):
    def test_funnel_counts_reached_stages(self):
        a = Student.objects.create(full_name="А")
        b = Student.objects.create(full_name="Б")
        for st in (Stage.CONTACTED, Stage.CALL, Stage.WAITING_PAYMENT, Stage.STUDYING):
            a.stage = st
            a.save()
        b.stage = Stage.LOST  # отвалился сразу — дошёл только до «Новая заявка»
        b.save()
        rows = {r["stage"]: r["count"] for r in funnel(Student.objects.all())}
        self.assertEqual(rows[Stage.NEW], 2)
        self.assertEqual(rows[Stage.STUDYING], 1)
        self.assertEqual(rows[Stage.JOB_SEARCH], 0)

    def test_dashboard_employment_rate(self):
        for i, stage in enumerate([Stage.JOB_SEARCH, Stage.OFFER, Stage.PROBATION_PASSED, Stage.STUDYING]):
            Student.objects.create(full_name=f"S{i}", stage=stage, job_salary=Decimal("100000"))
        m = dashboard_metrics(Student.objects.all())
        self.assertEqual(m["graduates"], 3)
        self.assertEqual(m["employed"], 2)
        self.assertEqual(m["employment_rate"], 67)
        self.assertEqual(m["avg_salary"], Decimal("100000"))


class AccessTests(BaseCase):
    def test_mentor_sees_only_own_students(self):
        mine = Student.objects.create(full_name="Мой", mentor=self.mentor)
        other = Student.objects.create(full_name="Чужой", mentor=self.other_mentor)
        self.client.login(username="mentor", password="pass12345")
        self.assertEqual(self.client.get(reverse("student_detail", args=[mine.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("student_detail", args=[other.pk])).status_code, 404)
        html = self.client.get(reverse("student_list")).content.decode()
        self.assertIn("Мой", html)
        self.assertNotIn("Чужой", html)

    def test_mentor_cannot_open_finance(self):
        self.client.login(username="mentor", password="pass12345")
        self.assertEqual(self.client.get(reverse("payments")).status_code, 403)
        self.assertEqual(self.client.get(reverse("partner_list")).status_code, 403)

    def test_partner_sees_only_own_report_with_masked_names(self):
        Student.objects.create(full_name="Петров Иван", partner=self.partner)
        other = Partner.objects.create(name="Другой")
        self.client.login(username="blogger", password="pass12345")
        self.assertRedirects(self.client.get("/"), reverse("partner_report", args=[self.partner.pk]))
        html = self.client.get(reverse("partner_report", args=[self.partner.pk])).content.decode()
        self.assertIn("Иван П.", html)
        self.assertNotIn("Петров Иван", html)
        self.assertEqual(self.client.get(reverse("partner_report", args=[other.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("board")).status_code, 403)

    def test_board_move(self):
        s = Student.objects.create(full_name="А", mentor=self.mentor)
        self.client.login(username="mentor", password="pass12345")
        r = self.client.post(reverse("board_move"), {"id": s.pk, "stage": "call"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        s.refresh_from_db()
        self.assertEqual(s.stage, Stage.CALL)

    def test_installments_split(self):
        s = Student.objects.create(full_name="А", mentor=self.mentor)
        self.client.login(username="mentor", password="pass12345")
        self.client.post(reverse("installments_add", args=[s.pk]), {
            "total": "100000", "parts": "3", "first_date": "2026-10-01",
        })
        amounts = list(s.payments.order_by("due_date").values_list("amount", flat=True))
        self.assertEqual(len(amounts), 3)
        self.assertEqual(sum(amounts), Decimal("100000"))
        dates = list(s.payments.order_by("due_date").values_list("due_date", flat=True))
        self.assertEqual([d.month for d in dates], [10, 11, 12])


@override_settings(LEAD_WEBHOOK_TOKEN="secret")
class LeadWebhookTests(BaseCase):
    url = "/api/leads/"

    def test_rejects_without_token(self):
        self.assertEqual(self.client.post(self.url, {"name": "X"}).status_code, 403)

    def test_creates_lead_and_links_partner_by_promo(self):
        r = self.client.post(self.url + "?token=secret", {
            "Name": "Сидоров Олег", "Phone": "+79990001122", "promo": "blog", "utm_source": "vk",
        })
        self.assertEqual(r.status_code, 201)
        s = Student.objects.get(phone="+79990001122")
        self.assertEqual(s.partner, self.partner)
        self.assertEqual(s.stage, Stage.NEW)

    def test_links_partner_by_utm_json(self):
        r = self.client.post(self.url, {"name": "Олег", "email": "o@example.com", "utm_source": "YouTube"},
                             content_type="application/json", HTTP_X_TOKEN="secret")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Student.objects.get(email="o@example.com").partner, self.partner)

    def test_duplicate_adds_note(self):
        self.client.post(self.url + "?token=secret", {"name": "А", "phone": "123"})
        r = self.client.post(self.url + "?token=secret", {"name": "А", "phone": "123"})
        self.assertTrue(r.json()["duplicate"])
        self.assertEqual(Student.objects.count(), 1)
        self.assertEqual(Student.objects.get().notes.count(), 1)

    def test_tilda_test_request(self):
        r = self.client.post(self.url + "?token=secret", {"test": "test"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Student.objects.count(), 0)


def sample_backup():
    return {
        "format": "crm-menti-backup", "version": 1, "exportedAt": "2026-10-02T20:00:00.000Z",
        "data": {
            "cohorts": [{"id": "c1", "name": "Поток 2", "start": "2026-09-01", "end": "2026-12-01",
                         "price": 100000, "active": True}],
            "team": [{"id": "t1", "name": "Ирина Котова", "userId": "u_abc"}],
            "partners": [{"id": "p1", "name": "Блог", "share": 40, "promo": "BLOG", "utm": "youtube", "active": True}],
            "payouts": [{"id": "po1", "partnerId": "p1", "amount": 5000, "date": "2026-09-20", "comment": "Сентябрь"}],
            "reports": [{"id": "p1", "name": "Блог"}],
            "students": [
                {
                    "id": "s1", "name": "Иванов Пётр", "phone": "+7 900 000-00-01", "telegram": "@ivanov",
                    "email": "", "city": "Казань", "consent": True, "stage": "offer",
                    "stageAt": "2026-09-25T10:00:00.000Z", "createdAt": "2026-08-01T09:00:00.000Z",
                    "cohortId": "c1", "mentorId": "t1", "price": 100000,
                    "partnerId": "p1", "partnerShare": 30, "promo": "BLOG", "utmSource": "youtube",
                    "jobCompany": "Рарус", "jobPosition": "Аналитик 1С", "jobSalary": 95000, "offerDate": "2026-09-25",
                    "history": [
                        {"from": None, "to": "new", "at": "2026-08-01T09:00:00.000Z", "by": None},
                        {"from": "new", "to": "studying", "at": "2026-08-10T09:00:00.000Z", "by": "u_abc"},
                        {"from": "studying", "to": "offer", "at": "2026-09-25T10:00:00.000Z", "by": "u_abc"},
                    ],
                    "payments": [
                        {"id": "x1", "amount": 50000, "due": "2026-08-05", "paid": "2026-08-05", "comment": "1/2"},
                        {"id": "x2", "amount": 50000, "due": "2026-09-05", "paid": None, "comment": "2/2"},
                    ],
                    "apps": [{"id": "a1", "company": "Рарус", "position": "Аналитик 1С", "status": "accepted",
                              "salary": 95000, "date": "2026-09-20", "interview": "2026-09-22T15:00", "url": "", "notes": ""}],
                    "notes": [{"id": "n1", "text": "Созвонились", "at": "2026-08-02T12:00:00.000Z", "by": "u_abc"}],
                },
                {"id": "s2", "name": "Пример Примеров", "stage": "new", "createdAt": "2026-09-01T09:00:00.000Z",
                 "demo": True, "history": [], "payments": [], "apps": [], "notes": []},
            ],
        },
    }


class BackupImportTests(BaseCase):
    def test_import_creates_everything(self):
        stats = import_backup(sample_backup())
        self.assertEqual(stats["students"]["created"], 1)  # запись с пометкой «пример» пропущена
        s = Student.objects.get(external_id="s1")
        self.assertEqual((s.full_name, s.stage, s.cohort.name), ("Иванов Пётр", Stage.OFFER, "Поток 2"))
        self.assertEqual(s.mentor.get_full_name(), "Ирина Котова")
        self.assertTrue(s.mentor.groups.filter(name="Менторы").exists())
        self.assertFalse(s.mentor.has_usable_password())
        self.assertEqual(s.partner_share_percent, Decimal("30"))  # доля из копии, а не текущая у партнёра
        self.assertEqual(s.created_at.date().isoformat(), "2026-08-01")
        self.assertEqual(s.history.count(), 3)
        self.assertEqual(s.history.first().changed_by, s.mentor)
        self.assertEqual(s.total_paid, Decimal("50000"))
        self.assertEqual(s.payments.filter(status=Payment.Status.PLANNED).count(), 1)
        app = s.applications.get()
        self.assertEqual((app.status, app.salary), ("accepted", Decimal("95000")))
        self.assertEqual(s.notes.get().author, s.mentor)
        partner = Partner.objects.get(external_id="p1")
        self.assertEqual(partner.accrued(), Decimal("15000.00"))
        self.assertEqual(partner.balance(), Decimal("10000.00"))

    def test_reimport_is_idempotent(self):
        import_backup(sample_backup())
        stats = import_backup(sample_backup())
        self.assertEqual(stats["students"], {"created": 0, "updated": 0, "skipped": 1})
        self.assertEqual((Student.objects.count(), Cohort.objects.count(), PartnerPayout.objects.count()), (1, 1, 1))
        self.assertEqual(Payment.objects.count(), 2)

    def test_overwrite_replaces_student(self):
        import_backup(sample_backup())
        data = sample_backup()
        data["data"]["students"][0]["stage"] = "probation_passed"
        data["data"]["students"][0]["payments"][1]["paid"] = "2026-09-06"
        stats = import_backup(data, overwrite=True)
        self.assertEqual(stats["students"]["updated"], 1)
        s = Student.objects.get(external_id="s1")
        self.assertEqual(s.stage, Stage.PROBATION_PASSED)
        self.assertEqual((s.payments.count(), s.total_paid), (2, Decimal("100000")))
        self.assertEqual(s.history.count(), 3)

    def test_real_student_keeps_demo_cohort_and_partner(self):
        data = sample_backup()
        for key in ("cohorts", "team", "partners"):
            data["data"][key][0]["demo"] = True
        import_backup(data)
        s = Student.objects.get(external_id="s1")
        self.assertEqual((s.cohort.name, s.partner.name, s.mentor.first_name), ("Поток 2", "Блог", "Ирина"))

    def test_next_step_from_version_2(self):
        data = sample_backup()
        data["version"] = 2
        data["data"]["students"][0]["next"] = {"text": "Узнать итог собеседования", "date": "2026-10-05"}
        import_backup(data)
        s = Student.objects.get(external_id="s1")
        self.assertEqual((s.next_step, s.next_step_date.isoformat()), ("Узнать итог собеседования", "2026-10-05"))

    def test_missing_or_null_next_step(self):
        data = sample_backup()
        data["data"]["students"][0]["next"] = None
        import_backup(data)
        s = Student.objects.get(external_id="s1")
        self.assertEqual((s.next_step, s.next_step_date), ("", None))

    def test_program_and_progress_from_version_3(self):
        data = sample_backup()
        data["version"] = 3
        data["data"]["cohorts"][0]["modules"] = [{"id": "m1", "title": "Введение"}, {"id": "m2", "title": "ТЗ"}, {"bad": 1}]
        data["data"]["students"][0]["progress"] = {"m1": "2026-09-10", "m2": None}
        import_backup(data)
        s = Student.objects.get(external_id="s1")
        self.assertEqual(s.cohort.modules, [{"id": "m1", "title": "Введение"}, {"id": "m2", "title": "ТЗ"}])
        self.assertEqual(s.progress, {"m1": "2026-09-10"})
        self.assertEqual(s.progress_summary, (1, 2))

    def test_program_added_to_cohort_imported_earlier(self):
        import_backup(sample_backup())
        data = sample_backup()
        data["version"] = 3
        data["data"]["cohorts"][0]["modules"] = [{"id": "m1", "title": "Введение"}]
        stats = import_backup(data)
        self.assertEqual(stats["cohorts"]["updated"], 1)
        self.assertEqual(Cohort.objects.get(external_id="c1").modules, [{"id": "m1", "title": "Введение"}])

    def test_old_backup_has_no_program(self):
        import_backup(sample_backup())
        s = Student.objects.get(external_id="s1")
        self.assertEqual((s.cohort.modules, s.progress, s.progress_summary), ([], {}, None))

    def test_meetings_trash_and_money_log_from_version_4(self):
        raw = sample_backup()
        raw["version"] = 4
        data = raw["data"]
        data["students"][0]["log"] = [{"id": "l1", "at": "2026-08-05T10:00:00.000Z", "by": "u_abc",
                                       "text": "Платёж отмечен оплаченным: 50 000 ₽"}]
        data["students"].append({"id": "s3", "name": "Удалённый Олег", "stage": "new",
                                 "createdAt": "2026-09-01T09:00:00.000Z", "deletedAt": "2026-09-02T09:00:00.000Z"})
        data["meetings"] = [
            {"id": "mt1", "title": "Разбор домашки", "kind": "call", "status": "done", "date": "2026-09-10",
             "time": "15:30", "duration": 45, "studentId": "s1", "cohortId": None, "mentorId": "t1",
             "link": "https://example.com/room", "notes": ""},
            {"id": "mt2", "title": "Занятие потока", "kind": "lesson", "status": "planned", "date": "2026-10-10",
             "time": "", "duration": None, "studentId": None, "cohortId": "c1", "mentorId": None},
            {"id": "mt3", "title": "Созвон", "kind": "что-то", "date": "2026-10-11", "studentId": "s3"},
            {"id": "mt4", "title": "Без даты", "date": ""},
        ]
        stats = import_backup(raw)
        self.assertFalse(Student.objects.filter(external_id="s3").exists())
        self.assertTrue(any("корзине" in w for w in stats["warnings"]))
        self.assertEqual(stats["meetings"], {"created": 2, "updated": 0, "skipped": 2})
        m = Meeting.objects.get(external_id="mt1")
        self.assertEqual((m.student.full_name, m.status, m.time.isoformat(), m.duration_min),
                         ("Иванов Пётр", "done", "15:30:00", 45))
        self.assertEqual(m.mentor.get_full_name(), "Ирина Котова")
        group = Meeting.objects.get(external_id="mt2")
        self.assertEqual((group.cohort.name, group.time, group.student), ("Поток 2", None, None))
        s = Student.objects.get(external_id="s1")
        self.assertTrue(s.notes.filter(text__startswith="Деньги: Платёж отмечен").exists())
        self.assertEqual(import_backup(raw)["meetings"]["created"], 0)  # повторный импорт не дублирует

    def test_with_demo(self):
        stats = import_backup(sample_backup(), with_demo=True)
        self.assertEqual(stats["students"]["created"], 2)

    def test_rejects_foreign_file(self):
        for bad in (b"not json", b'{"format": "other"}', b'{"format": "crm-menti-backup", "version": 99, "data": {}}'):
            with self.assertRaises(BackupError):
                import_backup(bad)
        self.assertEqual(Student.objects.count(), 0)

    def test_upload_page_admin_only(self):
        import json
        from django.core.files.uploadedfile import SimpleUploadedFile
        url = reverse("backup_import")
        self.client.login(username="mentor", password="pass12345")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.login(username="admin", password="pass12345")
        f = SimpleUploadedFile("backup.json", json.dumps(sample_backup()).encode(), content_type="application/json")
        r = self.client.post(url, {"file": f})
        self.assertContains(r, "Студенты: добавлено 1")
        r = self.client.post(url, {"file": SimpleUploadedFile("x.json", b"oops")})
        self.assertContains(r, "Файл не является JSON")
