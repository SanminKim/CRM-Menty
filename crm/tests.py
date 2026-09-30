from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.models import JobApplication, Partner, PartnerPayout, Payment, Stage, Student
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
