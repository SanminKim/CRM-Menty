import json

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings

from backend import store
from backend.backup import BackupError, import_backup
from backend.models import Account, Doc, LeadLog, State, Tombstone


def student(mentor=None, **extra):
    return {"name": "Иванов Пётр", "stage": "new", "mentorId": mentor, "payments": [], "notes": [],
            "progress": {}, **extra}


class BaseCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("admin", password="pass-12345-x")
        Account.objects.create(user=cls.admin, role="admin", link_id="t0")
        cls.mentor = User.objects.create_user("mentor", password="pass-12345-x", first_name="Ирина", last_name="Котова")
        Account.objects.create(user=cls.mentor, role="mentor", link_id="t1")
        cls.other = User.objects.create_user("other", password="pass-12345-x")
        Account.objects.create(user=cls.other, role="mentor", link_id="t2")
        cls.blogger = User.objects.create_user("blogger", password="pass-12345-x")
        Account.objects.create(user=cls.blogger, role="partner", link_id="p1")
        cls.nobody = User.objects.create_user("nobody", password="pass-12345-x")
        for doc_id, name in (("t0", "Админ"), ("t1", "Ирина Котова"), ("t2", "Алексей Дёмин")):
            store.system_write("team", doc_id, {"name": name})
        store.system_write("partners", "p1", {"name": "Блог", "share": 40, "promo": "BLOG", "utm": "youtube",
                                              "active": True, "contacts": "карта 0000"})
        store.system_write("cohorts", "c1", {"name": "Поток 2", "start": "2020-01-01", "active": True})
        store.system_write("students", "s1", student("t1", partnerId="p1", partnerShare=40))
        store.system_write("students", "s2", student("t2", name="Чужой Студент"))
        store.system_write("payouts", "po1", {"partnerId": "p1", "amount": 5000})
        store.system_write("reports", "p1", {"name": "Блог", "revenue": 1})
        store.system_write("reports", "p2", {"name": "Другой", "revenue": 2})
        store.system_write("meetings", "m1", {"title": "Созвон", "studentId": "s1", "mentorId": "t1"})
        store.system_write("meetings", "m4", {"title": "Консультация", "studentId": "s1", "mentorId": None})
        store.system_write("meetings", "m2", {"title": "Чужой созвон", "studentId": "s2", "mentorId": "t2"})
        store.system_write("meetings", "m3", {"title": "Занятие потока", "studentId": None, "cohortId": "c1", "mentorId": "t2"})

    def login(self, name):
        self.client.logout()
        self.assertTrue(self.client.login(username=name, password="pass-12345-x"))

    def sync(self, **params):
        res = self.client.get("/api/sync/", params)
        self.assertEqual(res.status_code, 200)
        return res.json()

    def ids(self, payload, collection):
        return sorted(d["id"] for d in payload["docs"].get(collection, []))

    def send(self, method, collection, doc_id, data=None):
        return getattr(self.client, method)(f"/api/db/{collection}/{doc_id}/", data=json.dumps(data or {}),
                                            content_type="application/json")


class AccessTests(BaseCase):
    def test_anonymous_gets_401_and_page_redirects_to_login(self):
        self.assertEqual(self.client.get("/api/sync/").status_code, 401)
        self.assertEqual(self.client.put("/api/db/students/x/", data="{}", content_type="application/json").status_code, 401)
        res = self.client.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login/", res["Location"])

    def test_user_without_role_has_no_access(self):
        self.login("nobody")
        self.assertEqual(self.client.get("/api/sync/").status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_admin_sees_everything(self):
        self.login("admin")
        data = self.sync()
        self.assertTrue(data["full"])
        self.assertEqual(self.ids(data, "students"), ["s1", "s2"])
        self.assertEqual(self.ids(data, "payouts"), ["po1"])
        self.assertEqual(self.ids(data, "reports"), ["p1", "p2"])

    def test_mentor_sees_only_own_students_and_no_money_of_school(self):
        self.login("mentor")
        data = self.sync()
        self.assertEqual(self.ids(data, "students"), ["s1"])
        self.assertEqual(self.ids(data, "meetings"), ["m1", "m3", "m4"])  # встречи своих студентов и занятие группы
        self.assertNotIn("payouts", data["docs"])
        self.assertNotIn("reports", data["docs"])
        partner = data["docs"]["partners"][0]["data"]
        self.assertEqual(partner["name"], "Блог")
        self.assertNotIn("contacts", partner)
        self.assertNotIn("Чужой", json.dumps(data, ensure_ascii=False))

    def test_partner_sees_only_own_report(self):
        self.login("blogger")
        data = self.sync()
        self.assertEqual(list(data["docs"]), ["reports"])
        self.assertEqual(self.ids(data, "reports"), ["p1"])
        self.assertEqual(self.send("put", "reports", "p1", {"name": "x"}).status_code, 403)
        self.assertEqual(self.send("patch", "students", "s1", {"name": "x"}).status_code, 403)
        self.assertEqual(self.client.get("/api/profiles/", {"ids": f"u{self.mentor.pk}"}).json(), {})
        self.assertEqual(self.client.get("/api/accounts/").status_code, 403)

    def test_mentor_cannot_touch_other_students(self):
        self.login("mentor")
        self.assertEqual(self.send("patch", "students", "s2", {"name": "Взлом"}).status_code, 403)
        self.assertEqual(self.send("put", "students", "s2", student("t1")).status_code, 403)
        self.assertEqual(self.send("patch", "students", "s1", {"mentorId": "t2"}).status_code, 403)  # отдать нельзя
        self.assertEqual(self.send("put", "students", "new1", student("t2")).status_code, 403)
        self.assertEqual(self.send("delete", "students", "s1").status_code, 403)
        self.assertEqual(self.send("patch", "meetings", "m2", {"title": "x"}).status_code, 403)
        self.assertEqual(self.send("patch", "meetings", "m3", {"title": "x"}).status_code, 403)
        self.assertEqual(self.send("patch", "meetings", "m1", {"studentId": "s2", "mentorId": "t2"}).status_code, 403)
        for collection, doc_id in (("cohorts", "c1"), ("team", "t1"), ("partners", "p1"), ("payouts", "po1"),
                                   ("reports", "p1"), ("config", "main")):
            self.assertEqual(self.send("put", collection, doc_id, {"name": "x"}).status_code, 403, collection)
        self.assertEqual(Doc.objects.get(collection="students", doc_id="s2").data["name"], "Чужой Студент")

    def test_mentor_cannot_plant_meetings_or_change_partner_terms(self):
        self.login("mentor")
        # встреча «от своего имени» с чужим студентом
        self.assertEqual(self.send("put", "meetings", "evil", {"title": "x", "studentId": "s2", "mentorId": "t1"}).status_code, 403)
        self.assertEqual(self.send("put", "meetings", "evil2", {"title": "x", "studentId": "s1", "mentorId": "t2"}).status_code, 403)
        self.assertEqual(self.send("put", "meetings", "group", {"title": "x", "studentId": None, "mentorId": None}).status_code, 403)
        # источник и доля партнёра у своего студента
        for patch in ({"partnerId": None}, {"partnerShare": 0}, {"partnerShare": 90}, {"demo": True}):
            self.assertEqual(self.send("patch", "students", "s1", patch).status_code, 403, patch)
        self.assertEqual(self.send("put", "students", "n1", student("t1", partnerId="p1", partnerShare=5)).status_code, 403)
        self.assertEqual(self.send("put", "students", "n2", student("t1", partnerId="nope", partnerShare=0)).status_code, 403)
        self.assertEqual(self.send("put", "students", "n3", student("t1", partnerId="p1", partnerShare=40)).status_code, 200)
        self.assertEqual(self.send("put", "students", "n4", student("t1")).status_code, 200)

    def test_missing_and_forbidden_look_the_same_for_non_admins(self):
        rev = State.objects.get(pk=1).rev
        self.login("mentor")
        self.assertEqual(self.send("patch", "students", "missing", {"a": 1}).status_code, 403)
        self.assertEqual(self.send("delete", "students", "missing").status_code, 403)
        self.assertEqual(self.send("delete", "meetings", "missing").status_code, 403)
        self.login("blogger")
        self.assertEqual(self.send("delete", "students", "missing").status_code, 403)
        self.assertEqual(self.send("put", "students", "x1", student("t1")).status_code, 403)
        self.login("admin")
        self.assertEqual(self.send("delete", "students", "missing").status_code, 200)
        self.assertEqual(State.objects.get(pk=1).rev, rev)  # пустые удаления не двигают ревизию

    def test_mentor_works_with_own_students(self):
        self.login("mentor")
        self.assertEqual(self.send("patch", "students", "s1", {"city": "Казань"}).status_code, 200)
        self.assertEqual(self.send("put", "students", "new1", student("t1", name="Новая Заявка")).status_code, 200)
        self.assertEqual(self.send("put", "meetings", "m9", {"title": "Разбор", "studentId": "s1", "mentorId": "t1"}).status_code, 200)
        self.assertEqual(self.send("patch", "students", "s1", {"deletedAt": "2026-10-03T10:00:00Z"}).status_code, 200)

    def test_bad_requests(self):
        self.login("admin")
        self.assertEqual(self.send("put", "secrets", "x", {}).status_code, 400)
        self.assertEqual(self.send("put", "students", "bad id!", {}).status_code, 400)
        self.assertEqual(self.send("put", "students", "a" * 65, {}).status_code, 400)
        self.assertEqual(self.send("put", "config", "other", {}).status_code, 400)
        self.assertEqual(self.send("patch", "students", "missing", {"a": 1}).status_code, 404)
        self.assertEqual(self.client.put("/api/db/students/s1%0A/", data="{}", content_type="application/json").status_code, 400)
        self.assertEqual(self.send("put", "students", "nul", {"name": "a\u0000b"}).status_code, 400)
        deep = {}
        for _ in range(40):
            deep = {"a": deep}
        self.assertEqual(self.send("put", "students", "deep", deep).status_code, 400)
        res = self.client.put("/api/db/students/s1/", data="[" * 100000, content_type="application/json")
        self.assertEqual(res.status_code, 400)
        res = self.client.put("/api/db/students/s1/", data="[1]", content_type="application/json")
        self.assertEqual(res.status_code, 400)
        big = {"text": "я" * (store.MAX_DOC_BYTES // 2 + 10)}
        self.assertEqual(self.send("put", "students", "big", big).status_code, 400)

    def test_csrf_is_required_for_writes(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.login(username="admin", password="pass-12345-x")
        res = client.put("/api/db/students/s1/", data="{}", content_type="application/json")
        self.assertEqual(res.status_code, 403)


class StoreTests(BaseCase):
    def test_update_merges_objects_and_replaces_arrays(self):
        self.login("admin")
        self.send("put", "students", "s9", student("t1", progress={"m1": "2026-01-01"}, payments=[{"id": "a"}], next={"text": "x"}))
        res = self.send("patch", "students", "s9", {"progress": {"m2": "2026-02-02"}, "payments": [{"id": "b"}], "next": None})
        data = res.json()["doc"]["data"]
        self.assertEqual(data["progress"], {"m1": "2026-01-01", "m2": "2026-02-02"})
        self.assertEqual(data["payments"], [{"id": "b"}])
        self.assertIsNone(data["next"])
        self.assertEqual(data["name"], "Иванов Пётр")
        self.assertNotIn("id", Doc.objects.get(doc_id="s9").data)

    def test_incremental_sync(self):
        self.login("admin")
        first = self.sync()
        same = self.sync(since=first["rev"], epoch=first["epoch"])
        self.assertFalse(same["full"])
        self.assertEqual(same["docs"], {})
        self.send("patch", "students", "s1", {"city": "Тверь"})
        self.send("delete", "payouts", "po1")
        later = self.sync(since=first["rev"], epoch=first["epoch"])
        self.assertEqual(self.ids(later, "students"), ["s1"])
        self.assertEqual(later["removed"], {"payouts": ["po1"]})
        self.assertGreater(later["rev"], first["rev"])
        self.assertTrue(self.sync(since=first["rev"], epoch=first["epoch"] + 5)["full"])

    def test_reassigned_student_disappears_for_old_mentor_with_meetings(self):
        self.login("mentor")
        first = self.sync()
        self.login("admin")
        self.send("patch", "students", "s1", {"mentorId": "t2"})
        self.login("mentor")
        later = self.sync(since=first["rev"], epoch=first["epoch"])
        self.assertEqual(self.ids(later, "meetings"), ["m1"])  # встречу, которую он проводит сам, видит по-прежнему
        self.assertEqual(later["removed"]["students"], ["s1"])
        self.assertEqual(later["removed"]["meetings"], ["m4"])
        self.login("other")
        self.assertEqual(self.ids(self.sync(), "students"), ["s1", "s2"])

    def test_incremental_sync_does_not_leak_ids_to_partner(self):
        self.login("blogger")
        first = self.sync()
        store.system_write("students", "s77", student("t1"))
        store.system_write("reports", "p2", {"name": "Другой", "revenue": 3})
        store.delete(store.Access(self.admin), "reports", "p2")
        Access = store.Access(self.admin)
        store.delete(Access, "students", "s2")
        later = self.sync(since=first["rev"], epoch=first["epoch"])
        self.assertEqual(later["docs"], {})
        self.assertEqual(later["removed"], {})

    def test_recreated_doc_drops_tombstone(self):
        self.login("admin")
        self.send("delete", "payouts", "po1")
        self.assertTrue(Tombstone.objects.filter(doc_id="po1").exists())
        self.send("put", "payouts", "po1", {"partnerId": "p1", "amount": 1})
        self.assertFalse(Tombstone.objects.filter(doc_id="po1").exists())

    def test_team_user_link_comes_from_accounts(self):
        self.login("admin")
        team = {d["id"]: d["data"] for d in self.sync()["docs"]["team"]}
        self.assertEqual(team["t1"]["userId"], f"u{self.mentor.pk}")
        self.send("patch", "team", "t1", {"userId": "u999", "name": "Ирина К."})
        self.assertNotIn("userId", Doc.objects.get(collection="team", doc_id="t1").data)

    def test_me_and_profiles(self):
        self.login("mentor")
        me = self.client.get("/api/me/").json()
        self.assertEqual((me["role"], me["isOwner"], me["linkId"], me["server"]), ("mentor", False, "t1", True))
        names = self.client.get("/api/profiles/", {"ids": f"u{self.mentor.pk},u{self.blogger.pk},u{self.nobody.pk},u_old,zzz,u²,u{'9' * 30}"}).json()
        self.assertEqual(names, {f"u{self.mentor.pk}": {"name": "Ирина Котова"}})  # партнёры и люди без роли не раскрываются


class AccountTests(BaseCase):
    def test_admin_creates_and_edits_accounts(self):
        self.login("admin")
        epoch = State.objects.get(pk=1).epoch
        res = self.client.post("/api/accounts/", data=json.dumps({
            "username": "newmentor", "name": "Олег Новый", "role": "mentor", "linkId": "t0", "password": "x"}),
            content_type="application/json")
        self.assertEqual(res.status_code, 400)  # t0 занята администратором
        store.system_write("team", "t3", {"name": "Олег Новый"})
        bad = {"username": "newmentor", "name": "Олег Новый", "role": "mentor", "linkId": "t3", "password": "korotkii"}
        self.assertEqual(self.client.post("/api/accounts/", data=json.dumps(bad), content_type="application/json").status_code, 400)
        good = {**bad, "password": "dlinnyi-parol-2026"}
        res = self.client.post("/api/accounts/", data=json.dumps(good), content_type="application/json")
        self.assertEqual(res.status_code, 201)
        user = User.objects.get(username="newmentor")
        self.assertEqual((user.account.role, user.account.link_id, user.get_full_name()), ("mentor", "t3", "Олег Новый"))
        self.assertGreater(State.objects.get(pk=1).epoch, epoch)
        res = self.client.patch(f"/api/accounts/{user.pk}/", data=json.dumps({"active": False}), content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.client.login(username="newmentor", password="dlinnyi-parol-2026"))
        listed = self.client.get("/api/accounts/").json()["accounts"]
        self.assertIn("newmentor", [a["username"] for a in listed])
        self.assertNotIn("password", json.dumps(listed))
        self.assertEqual(self.client.post("/api/accounts/", data=json.dumps({**good, "linkId": "t3", "username": "NEWMENTOR"}),
                                          content_type="application/json").status_code, 400)

    def test_own_password_change_keeps_session_and_demotion_cuts_access(self):
        self.login("admin")
        res = self.client.patch(f"/api/accounts/{self.admin.pk}/", data=json.dumps({"password": "novyi-dlinnyi-parol"}), content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get("/api/me/").status_code, 200)
        self.client.patch(f"/api/accounts/{self.mentor.pk}/", data=json.dumps({"active": False}), content_type="application/json")
        from django.test import Client
        other = Client()
        self.assertFalse(other.login(username="mentor", password="pass-12345-x"))

    def test_admin_cannot_lock_himself_out(self):
        self.login("admin")
        for patch in ({"active": False}, {"role": "mentor", "linkId": "t0"}):
            res = self.client.patch(f"/api/accounts/{self.admin.pk}/", data=json.dumps(patch), content_type="application/json")
            self.assertEqual(res.status_code, 400)

    def test_mentor_cannot_manage_accounts(self):
        self.login("mentor")
        self.assertEqual(self.client.get("/api/accounts/").status_code, 403)
        self.assertEqual(self.client.post("/api/accounts/", data="{}", content_type="application/json").status_code, 403)
        self.assertEqual(self.client.patch(f"/api/accounts/{self.admin.pk}/", data="{}", content_type="application/json").status_code, 403)


class PageTests(BaseCase):
    def test_page_is_served_with_server_adapter_and_headers(self):
        self.login("mentor")
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn("window.crmServer", html)
        self.assertIn("CRM Менти", html)
        csp = res["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("script-src 'unsafe-inline'", csp.replace("; ", ";\n"))
        self.assertEqual(csp.count("'sha256-"), 2)  # выполняются только два наших скрипта
        self.assertIn("script-src 'none'", self.client.get("/password/")["Content-Security-Policy"])
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        self.assertIn("csrftoken", res.cookies)

    def test_login_does_not_redirect_off_site(self):
        res = self.client.post("/login/", {"username": "mentor", "password": "pass-12345-x", "next": "//evil.example/"})
        self.assertEqual((res.status_code, res["Location"]), (302, "/"))

    def test_login_is_throttled(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for _ in range(8):
            self.assertEqual(self.client.post("/login/", {"username": "admin", "password": "wrong"}).status_code, 200)
        res = self.client.post("/login/", {"username": "admin", "password": "pass-12345-x"})
        self.assertEqual(res.status_code, 429)
        self.assertEqual(self.client.post("/login/", {"username": "mentor", "password": "pass-12345-x"}).status_code, 302)
        cache.clear()

    def test_backup_import_page_admin_only(self):
        self.login("mentor")
        self.assertEqual(self.client.get("/backup/import/").status_code, 403)
        self.login("admin")
        self.assertEqual(self.client.get("/backup/import/").status_code, 200)


@override_settings(LEAD_WEBHOOK_TOKEN="secret-token-0123456789")
class LeadTests(BaseCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def last_student(self):
        return Doc.objects.filter(collection="students").order_by("-rev").first()

    def test_rejects_without_token(self):
        self.assertEqual(self.client.post("/api/leads/", {"name": "А"}).status_code, 403)
        self.assertEqual(self.client.post("/api/leads/?token=wrong", {"name": "А"}).status_code, 403)

    @override_settings(LEAD_WEBHOOK_TOKEN="")
    def test_disabled_without_configured_token(self):
        self.assertEqual(self.client.post("/api/leads/?token=", {"name": "А"}).status_code, 403)

    def test_creates_lead_linked_to_partner_and_cohort(self):
        res = self.client.post("/api/leads/", {"name": "Мария Соколова", "phone": "+7 900 111-22-33", "promo": "blog",
                                               "tg": "sokolova"}, headers={"X-Token": "secret-token-0123456789"})
        self.assertEqual((res.status_code, res.json()), (200, {"ok": True}))
        data = self.last_student().data
        self.assertEqual((data["name"], data["partnerId"], data["partnerShare"], data["cohortId"], data["stage"]),
                         ("Мария Соколова", "p1", 40, "c1", "new"))
        self.assertEqual(data["telegram"], "@sokolova")
        self.assertEqual(data["next"]["text"], "Связаться")
        self.assertIsNone(data["mentorId"])
        self.assertTrue(LeadLog.objects.filter(ok=True, result="created").exists())

    def test_json_and_utm(self):
        res = self.client.post("/api/leads/?token=secret-token-0123456789", data=json.dumps({"email": "a@b.ru", "utm_source": "YouTube"}),
                               content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.last_student().data["partnerId"], "p1")
        self.assertTrue(self.last_student().data["createdAt"].endswith("Z"))

    def test_duplicate_adds_note(self):
        store.system_write("students", "s5", student("t1", phone="8 (900) 111-22-33"))
        res = self.client.post("/api/leads/?token=secret-token-0123456789", {"name": "Повтор", "phone": "+7 900 111 22 33"})
        self.assertEqual(res.json(), {"ok": True})  # ответ не выдаёт, что человек уже есть в базе
        self.assertIn("Повторная заявка", Doc.objects.get(doc_id="s5").data["notes"][-1]["text"])
        self.assertTrue(LeadLog.objects.filter(result="duplicate", student_id="s5").exists())

    @override_settings(LEAD_WEBHOOK_TOKEN="change-me")
    def test_placeholder_token_keeps_intake_off(self):
        self.assertEqual(self.client.post("/api/leads/?token=change-me", {"name": "А"}).status_code, 403)

    def test_consent_and_odd_values(self):
        send = lambda body: self.client.post("/api/leads/?token=secret-token-0123456789", data=json.dumps(body), content_type="application/json")
        self.assertEqual(send({"name": "Без Согласия", "consent": "false", "phone": ["x"]}).status_code, 200)
        self.assertFalse(self.last_student().data["consent"])
        self.assertEqual(send({"name": "С Согласием", "consent": "on", "email": "nul\u0000l@b.ru"}).status_code, 200)
        self.assertTrue(self.last_student().data["consent"])
        self.assertEqual(self.last_student().data["email"], "null@b.ru")

    def test_rate_limit(self):
        from backend import leads
        for i in range(leads.LEADS_PER_MINUTE):
            self.assertEqual(self.client.post("/api/leads/?token=secret-token-0123456789", {"name": f"Лид {i}"}).status_code, 200)
        self.assertEqual(self.client.post("/api/leads/?token=secret-token-0123456789", {"name": "Лишний"}).status_code, 429)

    def test_tilda_test_and_empty(self):
        self.assertEqual(self.client.post("/api/leads/?token=secret-token-0123456789", {"test": "test"}).json(), {"ok": True, "test": True})
        self.assertEqual(self.client.post("/api/leads/?token=secret-token-0123456789", {}).status_code, 400)


def sample_backup():
    return {
        "format": "crm-menti-backup", "version": 4, "exportedAt": "2026-10-02T20:00:00.000Z",
        "data": {
            "cohorts": [{"id": "bc1", "name": "Поток 3", "modules": [{"id": "m1", "title": "Введение"}]},
                        {"id": "bc2", "name": "Пример потока", "demo": True}],
            "team": [{"id": "bt1", "name": "Олег Тестов", "userId": "u_claude_abc"}],
            "partners": [{"id": "bp1", "name": "Канал", "share": 30, "demo": True}],
            "payouts": [{"id": "bpo1", "partnerId": "bp1", "amount": 100}, {"id": "bpo2", "partnerId": "nope", "amount": 1}],
            "reports": [{"id": "bp1", "name": "Канал"}],
            "meetings": [{"id": "bm1", "title": "Созвон", "date": "2026-10-10", "studentId": "bs1"},
                         {"id": "bm2", "title": "Созвон с примером", "date": "2026-10-10", "studentId": "bs2"}],
            "students": [
                {"id": "bs1", "name": "Петров Иван", "stage": "studying", "cohortId": "bc1", "mentorId": "bt1",
                 "partnerId": "bp1", "payments": [{"id": "x1", "amount": 50000, "due": "2026-08-05", "paid": "2026-08-05"}],
                 "notes": [{"id": "n1", "text": "Созвонились", "by": "u_claude_abc"}],
                 "log": [{"id": "l1", "text": "Платёж добавлен"}], "progress": {"m1": "2026-09-01"}},
                {"id": "bs2", "name": "Пример Примеров", "stage": "new", "demo": True},
                {"id": "bs3", "name": "Удалённый Олег", "stage": "new", "deletedAt": "2026-09-02T09:00:00.000Z"},
                {"id": "плохой id", "name": "Без нормального идентификатора"},
            ],
        },
    }


class BackupImportTests(BaseCase):
    def test_import_keeps_documents_as_they_are(self):
        stats = import_backup(sample_backup())
        self.assertEqual(stats["students"], {"created": 2, "updated": 0, "skipped": 0})
        s = Doc.objects.get(collection="students", doc_id="bs1").data
        self.assertEqual(s["payments"][0]["amount"], 50000)
        self.assertEqual(s["log"][0]["text"], "Платёж добавлен")
        self.assertEqual(s["progress"], {"m1": "2026-09-01"})
        self.assertTrue(Doc.objects.get(doc_id="bs3").data["deletedAt"])  # корзина переносится как корзина
        self.assertFalse(Doc.objects.filter(doc_id="bs2").exists())      # примеры пропущены
        self.assertFalse(Doc.objects.filter(doc_id="bc2").exists())
        self.assertTrue(Doc.objects.filter(collection="partners", doc_id="bp1").exists())  # нужен настоящему студенту
        self.assertEqual(stats["payouts"], {"created": 1, "updated": 0, "skipped": 1})
        self.assertEqual(stats["meetings"], {"created": 1, "updated": 0, "skipped": 1})
        self.assertFalse(Doc.objects.filter(collection="reports", doc_id="bp1").exists())

    def test_claude_authors_are_named_after_import(self):
        import_backup(sample_backup())
        team = Doc.objects.get(collection="team", doc_id="bt1").data
        self.assertEqual(team.get("legacyUserId"), "u_claude_abc")
        self.assertNotIn("userId", team)
        self.login("admin")
        self.assertEqual(self.client.get("/api/profiles/", {"ids": "u_claude_abc"}).json(), {"u_claude_abc": {"name": "Олег Тестов"}})

    def test_reimport_is_idempotent_and_overwrite_replaces(self):
        import_backup(sample_backup())
        Doc.objects.filter(doc_id="bs1").update(data={"name": "Правка на сервере", "mentorId": "bt1"})
        stats = import_backup(sample_backup())
        self.assertEqual(stats["students"], {"created": 0, "updated": 0, "skipped": 2})
        self.assertEqual(Doc.objects.get(doc_id="bs1").data["name"], "Правка на сервере")
        stats = import_backup(sample_backup(), overwrite=True)
        self.assertEqual(stats["students"]["updated"], 2)
        self.assertEqual(Doc.objects.get(doc_id="bs1").data["name"], "Петров Иван")

    def test_with_demo(self):
        stats = import_backup(sample_backup(), with_demo=True)
        self.assertEqual(stats["students"]["created"], 3)
        self.assertEqual(stats["meetings"]["created"], 2)

    def test_old_versions_and_foreign_files(self):
        raw = sample_backup()
        raw["version"] = 1
        del raw["data"]["meetings"]
        self.assertEqual(import_backup(raw)["students"]["created"], 2)
        for bad in ("не json", {"format": "other"}, {"format": "crm-menti-backup", "version": 99, "data": {}},
                    {"format": "crm-menti-backup", "version": 4}):
            with self.assertRaises(BackupError):
                import_backup(bad)

    def test_upload_through_page(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.login("admin")
        upload = SimpleUploadedFile("backup.json", json.dumps(sample_backup()).encode(), content_type="application/json")
        res = self.client.post("/backup/import/", {"file": upload})
        self.assertContains(res, "Студенты: добавлено 2")
