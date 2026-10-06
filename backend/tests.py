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
        self.assertEqual(self.ids(data, "reports"), ["p1", "p2", "t1", "t2"])   # отчёты блогера и менторов с доступом

    def test_mentor_sees_only_own_students_and_no_money_of_school(self):
        self.login("mentor")
        data = self.sync()
        self.assertEqual(self.ids(data, "students"), ["s1"])
        self.assertEqual(self.ids(data, "meetings"), ["m1", "m3", "m4"])  # встречи своих студентов и занятие группы
        self.assertNotIn("payouts", data["docs"])
        self.assertEqual(self.ids(data, "reports"), ["t1"])                # из отчётов — только свой
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

    def test_mentor_reads_only_own_report_and_no_money_collections(self):
        store.system_write("directions", "d1", {"name": "Аналитик 1С", "terms": [{"from": "2000-01", "parties": [
            {"kind": "partner", "id": "p1", "share": 40}, {"kind": "mentor", "id": "t1", "share": 30}, {"kind": "mentor", "id": "t2", "share": 5}]}]})
        store.system_write("expenses", "e1", {"directionId": "d1", "date": "2026-10-01", "amount": 500})
        store.system_write("payouts", "mp1", {"mentorId": "t1", "amount": 9000, "date": "2026-01-10"})
        store.system_write("payouts", "mp2", {"mentorId": "t2", "amount": 7000, "date": "2026-01-10"})
        self.login("mentor")
        data = self.sync()
        self.assertEqual(self.ids(data, "reports"), ["t1"])                # свой отчёт, без отчётов партнёра и другого ментора
        for collection in ("payouts", "directions", "expenses"):
            self.assertNotIn(collection, data["docs"])                      # условия сторон и расходы ментору не отдаются
        report = data["docs"]["reports"][0]["data"]
        self.assertEqual((report["kind"], report["paidOut"]), ("mentor", 9000))
        self.assertNotIn("7000", json.dumps(report))
        for collection, doc_id, body in (("payouts", "mp9", {"mentorId": "t1", "amount": 1}), ("directions", "d1", {"name": "x"}),
                                         ("expenses", "e9", {"amount": 1}), ("reports", "t1", {}), ("reports", "t2", {})):
            for method in ("put", "patch", "delete"):
                self.assertEqual(self.send(method, collection, doc_id, body).status_code, 403, (collection, method))
        self.login("other")
        self.assertEqual(self.ids(self.sync(), "reports"), ["t2"])
        self.login("blogger")
        data = self.sync()
        self.assertEqual(list(data["docs"]), ["reports"])
        self.assertEqual(self.ids(data, "reports"), ["p1"])

    def test_same_id_for_mentor_and_partner_does_not_open_the_wrong_report(self):
        store.system_write("team", "p1", {"name": "Двойник", "share": 77})   # запись команды с тем же id, что у партнёра
        twin = User.objects.create_user("twin", password="pass-12345-x")
        Account.objects.create(user=twin, role="mentor", link_id="p1")
        store.system_write("students", "s78", student("p1"))
        self.assertEqual(Doc.objects.get(collection="reports", doc_id="p1").data["kind"], "partner")
        self.login("twin")
        data = self.sync()
        self.assertNotIn("reports", data["docs"])                           # отчёт партнёра ментору не отдаётся
        self.assertNotIn("share", [d["data"] for d in data["docs"]["team"] if d["id"] == "p1"][0])

    def test_mentor_hears_only_about_own_report(self):
        self.login("mentor")
        first = self.sync()
        store.system_write("payouts", "mp3", {"mentorId": "t1", "amount": 100, "date": "2026-02-01"})
        store.system_write("payouts", "mp4", {"mentorId": "t2", "amount": 100, "date": "2026-02-01"})
        store.system_write("payouts", "po7", {"partnerId": "p1", "amount": 100, "date": "2026-02-01"})
        later = self.sync(since=first["rev"], epoch=first["epoch"])
        self.assertEqual(self.ids(later, "reports"), ["t1"])
        self.assertEqual(later["docs"]["reports"][0]["data"]["paidOut"], 100)
        self.assertEqual(later.get("removed"), {})                          # ни чужих выплат, ни чужих отчётов в списке исчезнувших

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
        store.system_write("students", "s76", student("t1"))                # отчёты пересчитаны: дальше меняется только то, что трогает тест
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
        store.system_write("students", "s76", student("t1"))                # отчёты пересчитаны заранее
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


class ReportTests(BaseCase):
    """Чистая выручка направления за месяц = оплаты студентов минус расходы; каждая сторона получает от неё свой процент."""

    def setUp(self):
        store.system_write("directions", "d1", {"name": "Аналитик 1С", "terms": [
            {"from": "2000-01", "parties": [{"kind": "partner", "id": "p1", "share": 40}, {"kind": "mentor", "id": "t1", "share": 30}]}]})
        store.system_write("cohorts", "c1", {"name": "Поток 2", "start": "2020-01-01", "active": True, "directionId": "d1"})
        self.login("admin")

    def report(self, doc_id="p1"):
        return Doc.objects.get(collection="reports", doc_id=doc_id).data

    def pay(self, doc_id, *payments, **extra):
        store.system_write("students", doc_id, student("t1", cohortId="c1", payments=[
            {"id": f"x{i}", "amount": amount, "paid": paid} for i, (amount, paid) in enumerate(payments)], **extra))

    def test_everyone_gets_a_share_of_net_revenue(self):
        self.pay("s1", (100000, "2026-10-03"), (50000, "2026-10-20"), (70000, None), partnerId="p1")
        self.pay("s2", (30000, "2026-10-05"))                                # пришёл не от блогера: в выручку направления входит
        store.system_write("expenses", "e1", {"directionId": "d1", "date": "2026-10-10", "amount": 20000, "comment": "Реклама"})
        store.system_write("expenses", "e2", {"directionId": "d1", "date": "2026-10-15", "amount": "10000"})
        blogger, mentor = self.report("p1"), self.report("t1")
        row = {"month": "2026-10", "directionId": "d1", "direction": "Аналитик 1С", "received": 180000, "expenses": 30000, "net": 150000}
        self.assertEqual(blogger["months"], [{**row, "share": 40, "accrued": 60000}])
        self.assertEqual(mentor["months"], [{**row, "share": 30, "accrued": 45000}])
        self.assertEqual((blogger["kind"], blogger["accrued"], blogger["paidOut"], blogger["balance"]), ("partner", 60000, 5000, 55000))
        self.assertEqual((mentor["kind"], mentor["name"], mentor["accrued"], mentor["balance"]), ("mentor", "Ирина Котова", 45000, 45000))

    def test_an_unforeseen_expense_lowers_everyones_share_and_can_be_removed(self):
        self.pay("s1", (100000, "2026-10-03"))
        self.assertEqual((self.report("p1")["accrued"], self.report("t1")["accrued"]), (40000, 30000))
        self.assertEqual(self.send("put", "expenses", "e1", {"directionId": "d1", "date": "2026-10-28", "amount": 25000, "comment": "Возврат оборудования"}).status_code, 200)
        self.assertEqual((self.report("p1")["accrued"], self.report("t1")["accrued"]), (30000, 22500))
        self.send("delete", "expenses", "e1")
        self.assertEqual((self.report("p1")["accrued"], self.report("t1")["accrued"]), (40000, 30000))

    def test_month_with_more_expenses_than_income_gives_negative_accrual(self):
        self.pay("s1", (100000, "2026-09-03"))
        store.system_write("expenses", "e1", {"directionId": "d1", "date": "2026-10-02", "amount": 10000})
        r = self.report("p1")
        self.assertEqual([(m["month"], m["net"], m["accrued"]) for m in r["months"]], [("2026-10", -10000, -4000), ("2026-09", 100000, 40000)])
        self.assertEqual(r["accrued"], 36000)                                # убыток месяца уменьшает общий остаток

    def test_new_terms_apply_from_their_month_and_keep_the_past(self):
        self.pay("s1", (100000, "2026-09-03"), (100000, "2026-10-03"), (100000, "2026-11-03"))
        self.send("patch", "directions", "d1", {"terms": [
            {"from": "2000-01", "parties": [{"kind": "partner", "id": "p1", "share": 40}, {"kind": "mentor", "id": "t1", "share": 30}]},
            {"from": "2026-10", "parties": [{"kind": "partner", "id": "p1", "share": 50}]}]})   # с октября ментора в составе нет
        self.assertEqual([(m["month"], m["share"], m["accrued"]) for m in self.report("p1")["months"]],
                         [("2026-11", 50, 50000), ("2026-10", 50, 50000), ("2026-09", 40, 40000)])
        self.assertEqual([(m["month"], m["accrued"]) for m in self.report("t1")["months"]], [("2026-09", 30000)])

    def test_each_direction_has_its_own_parties(self):
        store.system_write("directions", "d2", {"name": "Собеседования", "terms": [{"from": "2000-01", "parties": [{"kind": "mentor", "id": "t2", "share": 60}]}]})
        store.system_write("cohorts", "c2", {"name": "Интенсив", "directionId": "d2"})
        self.pay("s1", (100000, "2026-10-03"))
        store.system_write("students", "s2", student("t2", cohortId="c2", payments=[{"amount": 20000, "paid": "2026-10-04"}]))
        store.system_write("students", "s5", student("t2", payments=[{"amount": 7777, "paid": "2026-10-04"}]))   # без потока: ни в одно направление не входит
        self.assertEqual([(m["direction"], m["net"], m["accrued"]) for m in self.report("t2")["months"]], [("Собеседования", 20000, 12000)])
        self.assertEqual([(m["direction"], m["net"]) for m in self.report("p1")["months"]], [("Аналитик 1С", 100000)])

    def test_partner_report_keeps_traffic_numbers_without_contacts(self):
        self.pay("s1", (50000, "2026-08-05"), ("33333", "2026-09-05"), (10000, None), name="Иванов Пётр Сергеевич", partnerId="p1",
                 stage="offer", phone="+7 900 000-00-01", createdAt="2026-08-01T09:00:00.000Z", history=[{"to": "new"}, {"to": "studying"}, {"to": "offer"}])
        store.system_write("students", "s3", student("t2", name="Удалённая Анна", partnerId="p1", cohortId="c1",
                                                     deletedAt="2026-09-01T00:00:00Z", payments=[{"amount": 1000, "paid": "2026-08-01"}]))
        store.system_write("students", "s4", student("t2", name="Одноимённый", partnerId="p1", stage="lost",
                                                     createdAt="2026-09-01T09:00:00.000Z", history=[{"to": "new"}, {"to": "contacted"}, {"to": "lost"}]))
        self.assertEqual(self.send("put", "reports", "p1", {"revenue": 777777, "rows": [{"name": "Подделка"}]}).status_code, 200)
        r = self.report()
        self.assertEqual((r["name"], r["promo"], r["leads"], r["paidStudents"], r["conv"], r["employed"], r["revenue"]), ("Блог", "BLOG", 2, 1, 50, 1, 83333))
        self.assertEqual(r["rows"], [{"name": "Одноимённый", "stage": "lost", "date": "2026-09-01", "paid": 0},
                                     {"name": "Пётр И.", "stage": "offer", "date": "2026-08-01", "paid": 83333}])
        self.assertEqual([f["count"] for f in r["funnel"]], [2, 2, 1, 1, 1, 1, 1, 1, 0])
        self.assertEqual([(m["month"], m["received"], m["accrued"]) for m in r["months"]], [("2026-09", 33333, 13333.2), ("2026-08", 50000, 20000)])
        self.assertEqual(r["payouts"], [{"date": "", "amount": 5000, "comment": ""}])
        dumped = json.dumps(r, ensure_ascii=False)
        for secret in ("+7 900", "Сергеевич", "Удалённая", "Подделка", "777777", "карта 0000"):
            self.assertNotIn(secret, dumped)

    def test_mentor_edit_refreshes_reports_and_partner_gets_them_by_sync(self):
        store.system_write("students", "s1", student("t1", cohortId="c1", partnerId="p1", partnerShare=40))
        self.login("blogger")
        first = self.sync()
        self.login("mentor")
        self.send("patch", "students", "s1", {"payments": [{"id": "a", "amount": 20000, "paid": "2026-10-01"}]})
        self.assertEqual((self.report("p1")["accrued"], self.report("t1")["accrued"]), (8000, 6000))
        self.login("blogger")
        later = self.sync(since=first["rev"], epoch=first["epoch"])
        self.assertEqual(self.ids(later, "reports"), ["p1"])
        self.assertEqual(later["docs"]["reports"][0]["data"]["accrued"], 8000)

    def test_payouts_and_names_refresh_reports(self):
        self.send("put", "payouts", "po2", {"partnerId": "p1", "amount": 700, "date": "2026-10-02", "comment": "Октябрь"})
        self.send("put", "payouts", "mp1", {"mentorId": "t1", "amount": 300, "date": "2026-10-02"})
        self.assertEqual((self.report("p1")["paidOut"], self.report("t1")["paidOut"]), (5700, 300))
        self.assertEqual(self.report("p1")["payouts"][0], {"date": "2026-10-02", "amount": 700, "comment": "Октябрь"})
        self.send("delete", "payouts", "po2")
        self.assertEqual(self.report("p1")["paidOut"], 5000)
        self.send("patch", "partners", "p1", {"name": "Блог 2.0"})
        self.send("patch", "team", "t1", {"name": "Ирина К."})
        self.assertEqual((self.report("p1")["name"], self.report("t1")["name"]), ("Блог 2.0", "Ирина К."))

    def test_unrelated_edit_keeps_report_untouched(self):
        self.pay("s1", (1000, "2026-10-01"), partnerId="p1")
        rev = Doc.objects.get(collection="reports", doc_id="p1").rev
        self.send("patch", "students", "s2", {"city": "Тверь"})
        self.send("patch", "students", "s1", {"city": "Казань"})
        self.assertEqual(Doc.objects.get(collection="reports", doc_id="p1").rev, rev)

    def test_only_admin_writes_reports(self):
        self.send("put", "reports", "p1", {})
        for who in ("mentor", "blogger"):
            self.login(who)
            for method in ("put", "patch", "delete"):
                self.assertEqual(self.send(method, "reports", "p1", {"revenue": 1}).status_code, 403, (who, method))
        self.assertEqual(self.report()["revenue"], 0)

    def test_broken_data_never_breaks_the_reports(self):
        store.system_write("students", "s1", student("t1", cohortId="c1", partnerId="p1", partnerShare=40))
        self.login("mentor")
        weird = [
            {"payments": "не список", "history": [{"to": ["x"]}, {"to": {}}, "мусор"], "stage": ["x"]},
            {"payments": [{"amount": 10 ** 400, "paid": "2026-01-01"}, {"amount": 1e308, "paid": "x"}, {"amount": "1_000", "paid": "x"},
                          {"amount": ["x"], "paid": "x"}, "мусор", {"amount": "12.5", "paid": "2026-01-02"}, {"amount": 5, "paid": ["2026-01-03"]}]},
            {"createdAt": {"a": 1}, "name": ["Список"], "stage": "<b>x", "cohortId": ["c1"]}, {"cohortId": "c1"},
            {"name": "+7 900 123-45-67"}, {"name": "lead@example.com"},
        ]
        for patch in weird:
            self.assertEqual(self.send("patch", "students", "s1", patch).status_code, 200, patch)
        self.login("admin")
        for body in ({"terms": "x"}, {"terms": [{"from": 5}, "мусор", {"from": "2026-13x", "parties": 1}, {"from": "2020-01", "parties": [
                {"kind": "partner", "id": "p1", "share": "сорок"}, {"kind": "partner", "id": "p1", "share": 40}, {"kind": "owner", "id": "p1", "share": 9},
                {"kind": "mentor", "id": ["t1"], "share": 5}, {"kind": "mentor", "id": "t1", "share": 1e9}, "мусор"]}]}):
            self.assertEqual(self.send("patch", "directions", "d1", body).status_code, 200, body)
        for body in ({"directionId": ["d1"], "date": "2026-01-05", "amount": 2.5}, {"directionId": "d1", "date": 5, "amount": 1}, {"directionId": "d1", "date": "2026-01-06", "amount": "мусор"}):
            self.assertEqual(self.send("put", "expenses", f"e{len(str(body))}", body).status_code, 200, body)
        r = self.report()
        self.assertEqual((r["revenue"], r["rows"][0]["stage"], r["rows"][0]["date"], r["rows"][0]["name"]), (17.5, "", "", "Без имени"))
        self.assertEqual([(m["received"], m["share"], m["accrued"]) for m in r["months"]], [(12.5, 0, 0)])   # повтор стороны не считается дважды
        self.assertEqual([(m["share"], m["accrued"]) for m in self.report("t1")["months"]], [(100, 12.5)])   # ставка не выходит за 100%
        self.assertEqual(self.send("put", "students", "n9", student("t1", partnerId=[], name="Список вместо партнёра")).status_code, 200)

    def test_same_order_and_sums_as_the_page(self):
        from backend import reports
        amounts = [9999.9, 3703.5, 233.1, 30000.3]
        students = {f"s{i}": {"partnerId": "p", "cohortId": "c", "name": f"Студент {'АБВГ'[i]}", "createdAt": "2026-09-01T10:00:00.000Z",
                              "payments": [{"amount": v, "paid": f"2026-0{6 + i}-02"}]} for i, v in enumerate(amounts)}
        data = {"students": students, "cohorts": {"c": {"directionId": "d"}}, "partners": {"p": {"name": "Канал"}}, "team": {},
                "directions": {"d": {"name": "Курс", "terms": [{"from": "2000-01", "parties": [{"kind": "partner", "id": "p", "share": 33}]}]}},
                "expenses": {}, "payouts": {"a": {"partnerId": "p", "amount": 1, "date": "2026-09-01", "comment": "первая"},
                                            "b": {"partnerId": "p", "amount": 2, "date": "2026-09-01", "comment": "вторая"}}}
        r = reports.build("p", data)
        expected = 0
        for v in reversed(amounts):  # страница складывает начисления по порядку строк: от новых месяцев к старым
            expected += reports._round(v * 33) / 100
        self.assertEqual(r["accrued"], expected)
        self.assertEqual([x["comment"] for x in r["payouts"]], ["вторая", "первая"])  # при равных датах — обратный порядок записей
        self.assertEqual([x["name"] for x in r["rows"]], ["Г С.", "В С.", "Б С.", "А С."])

    def test_moving_and_trashing_students_updates_reports(self):
        store.system_write("partners", "p2", {"name": "Другой"})
        self.send("put", "reports", "p2", {})
        self.pay("s1", (1000, "2026-10-01"), partnerId="p1")
        self.assertEqual((self.report("p1")["revenue"], self.report("p2")["revenue"], self.report("p1")["accrued"]), (1000, 0, 400))
        self.send("patch", "students", "s1", {"partnerId": "p2"})
        self.assertEqual((self.report("p1")["revenue"], self.report("p2")["revenue"]), (0, 1000))
        self.assertEqual(self.report("p1")["accrued"], 400)                 # доля считается от выручки направления, а не от своих студентов
        self.send("patch", "students", "s1", {"deletedAt": "2026-10-03T00:00:00Z"})
        self.assertEqual((self.report("p2")["leads"], self.report("p1")["accrued"]), (0, 0))
        self.send("patch", "students", "s1", {"deletedAt": None})
        self.assertEqual((self.report("p2")["leads"], self.report("p1")["accrued"]), (1, 400))
        self.send("patch", "cohorts", "c1", {"directionId": None})          # поток вывели из направления
        self.assertEqual(self.report("p1")["accrued"], 0)

    def test_report_follows_accounts_and_removal(self):
        Doc.objects.filter(collection="reports").delete()
        self.send("patch", "students", "s1", {"city": "Тверь"})  # правка, от которой цифры не зависят, пересчёта не вызывает
        self.assertFalse(Doc.objects.filter(collection="reports").exists())
        self.send("patch", "students", "s1", {"stage": "contacted"})  # у blogger и менторов есть доступ, а отчётов нет: они создаются
        self.assertEqual(sorted(Doc.objects.filter(collection="reports").values_list("doc_id", flat=True)), ["p1", "t1", "t2"])
        self.send("delete", "partners", "p1")
        self.assertFalse(Doc.objects.filter(collection="reports", doc_id="p1").exists())
        self.assertTrue(Tombstone.objects.filter(collection="reports", doc_id="p1").exists())

    def test_stage_list_matches_the_page(self):
        import re
        from pathlib import Path
        from django.conf import settings
        from backend import reports
        page = (Path(settings.BASE_DIR) / "web" / "index.html").read_text(encoding="utf-8")
        block = page.split("const STAGES = [", 1)[1].split("];", 1)[0]
        on_page = tuple(re.findall(r'\["([a-z_]+)", "([^"]+)", "[a-z]+"\]', block))
        self.assertEqual(on_page, reports.STAGES)

    def test_report_for_unknown_party_is_rejected(self):
        self.assertEqual(self.send("put", "reports", "nope", {}).status_code, 400)

    def test_new_accounts_get_reports_at_once(self):
        store.system_write("partners", "p9", {"name": "Новый канал"})
        store.system_write("team", "t9", {"name": "Новый Ментор"})
        for username, role, link in (("newblog", "partner", "p9"), ("newmentor", "mentor", "t9")):
            res = self.client.post("/api/accounts/", data=json.dumps({
                "username": username, "role": role, "linkId": link, "password": "dlinnyi-parol-2026"}), content_type="application/json")
            self.assertEqual(res.status_code, 201)
        self.assertEqual((self.report("p9")["name"], self.report("t9")["name"]), ("Новый канал", "Новый Ментор"))
        self.client.logout()
        self.assertTrue(self.client.login(username="newblog", password="dlinnyi-parol-2026"))
        self.assertEqual(self.ids(self.sync(), "reports"), ["p9"])


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


class mock_limits:
    """Временно уменьшает общий предел заявок через бота."""

    def __init__(self, module, per_minute):
        self.module, self.value = module, per_minute

    def __enter__(self):
        self.old = self.module.LEADS_PER_MINUTE
        self.module.LEADS_PER_MINUTE = self.value

    def __exit__(self, *exc):
        self.module.LEADS_PER_MINUTE = self.old


@override_settings(TELEGRAM_BOT_TOKEN="123456:test-token", PUBLIC_URL="https://crm.example.test")
class TelegramTests(BaseCase):
    SECRET = None

    def setUp(self):
        from unittest import mock
        from backend import telegram
        cache.clear()
        self.addCleanup(cache.clear)
        self.sent = []
        patcher = mock.patch.object(telegram, "call", side_effect=lambda method, **kw: self.sent.append((method, kw)) or {"ok": True})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.secret = telegram.webhook_secret()
        st = store.state()
        st.bot_username = "menti_bot"
        st.save()

    def hook(self, update, secret=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/telegram/", data=json.dumps(update), content_type="application/json",
                                    headers={"X-Telegram-Bot-Api-Secret-Token": self.secret if secret is None else secret})

    def say(self, text, chat=555, username="vera_s", **extra):
        sender = {"id": chat, "is_bot": False, "first_name": "Вера"}
        if username:
            sender["username"] = username
        return self.hook({"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat, "type": "private"},
                                                      "from": sender, "text": text, **extra}})

    def press(self, chat=555, username="vera_s"):
        return self.hook({"update_id": 2, "callback_query": {
            "id": "cb1", "data": "consent", "from": {"id": chat, "username": username, "first_name": "Вера"},
            "message": {"message_id": 1, "chat": {"id": chat, "type": "private"}}}})

    def texts(self, chat=555):
        return [kw["text"] for method, kw in self.sent if method == "sendMessage" and kw["chat_id"] == chat]

    def lead(self):
        return Doc.objects.filter(collection="students", data__source="telegram").order_by("-rev").first()

    def test_rejects_wrong_secret_and_is_off_without_token(self):
        self.assertEqual(self.hook({"message": {}}, secret="wrong").status_code, 403)
        self.assertEqual(self.hook({"message": {}}, secret="").status_code, 403)
        with override_settings(TELEGRAM_BOT_TOKEN=""):
            self.assertEqual(self.hook({"message": {}}).status_code, 404)

    def test_full_lead_dialog_with_partner_link(self):
        from backend.models import TgChat
        TgChat.objects.create(chat_id=900, user=self.admin)       # администратор подключил уведомления
        TgChat.objects.create(chat_id=901, user=self.mentor)
        self.say("/start BLOG")
        self.assertIn("соглашаетесь", self.texts()[-1])
        self.assertFalse(self.lead())
        self.say("Вера Сайтова")                                   # без согласия дальше не идём
        self.assertIn("нажмите кнопку", self.texts()[-1])
        self.press()
        self.assertIn("Как к вам обращаться", self.texts()[-1])
        self.say("Сайтова Вера")
        self.assertIn("опыт", self.texts()[-1])
        self.say("Работаю бухгалтером, хочу в аналитики")
        data = self.lead().data
        self.assertEqual((data["name"], data["telegram"], data["partnerId"], data["partnerShare"], data["consent"]),
                         ("Сайтова Вера", "@vera_s", "p1", 40, True))
        self.assertEqual((data["comment"], data["stage"], data["tgId"], data["mentorId"], data["cohortId"]),
                         ("Работаю бухгалтером, хочу в аналитики", "new", 555, None, "c1"))
        self.assertEqual(data["next"]["text"], "Связаться")
        self.assertIn("Спасибо", self.texts()[-1])
        note = self.texts(900)[-1]
        self.assertIn("Новая заявка из Telegram: Сайтова Вера, @vera_s · Блог", note)
        self.assertIn("https://crm.example.test/", note)
        self.assertEqual(self.texts(901), [])                      # ментору о ничьей заявке не пишем
        # человек пишет ещё раз: дубль не создаётся, сообщение попадает заметкой
        count = Doc.objects.filter(collection="students").count()
        self.say("/start OTHER")
        self.assertIn("уже у нас", self.texts()[-1])
        self.say("Забыла сказать: могу только по вечерам")
        self.assertEqual(Doc.objects.filter(collection="students").count(), count)
        self.assertIn("Сообщение в боте: Забыла сказать", self.lead().data["notes"][-1]["text"])

    def test_without_username_bot_asks_for_phone(self):
        self.say("/start", username=None)
        self.press(username=None)
        self.say("Пётр Безников", username=None)
        self.assertIn("Оставьте телефон", self.texts()[-1])
        self.say("нет", username=None)
        self.assertIn("Оставьте телефон", self.texts()[-1])
        # чужой контакт, пересланный в чат, не принимается
        self.say("", username=None, contact={"phone_number": "+79990001122", "user_id": 777})
        self.assertIn("Оставьте телефон", self.texts()[-1])
        self.say("", username=None, contact={"phone_number": "+79990001122", "user_id": 555})
        self.say("Хочу сменить профессию", username=None)
        data = self.lead().data
        self.assertEqual((data["name"], data["phone"], data["telegram"], data["partnerId"]), ("Пётр Безников", "+79990001122", "", None))

    def test_existing_student_is_not_duplicated(self):
        store.system_write("students", "s7", student("t1", telegram="@Vera_S"))
        count = Doc.objects.filter(collection="students").count()
        self.say("/start BLOG"); self.press(); self.say("Вера"); self.say("Вернулась")
        self.assertEqual(Doc.objects.filter(collection="students").count(), count)
        self.assertIn("Повторная заявка из Telegram: Вернулась", Doc.objects.get(doc_id="s7").data["notes"][-1]["text"])

    def test_ignores_groups_bots_junk_and_floods(self):
        from backend import telegram
        from backend.models import TgChat
        self.hook({"message": {"chat": {"id": -100, "type": "group"}, "from": {"id": 1}, "text": "/start"}})
        self.hook({"message": {"chat": {"id": 5, "type": "private"}, "from": {"id": 5, "is_bot": True}, "text": "/start"}})
        for junk in ([], "x", {"message": "x"}, {"message": {"chat": "x"}}, {"callback_query": {"id": 1}},
                     {"message": {"chat": {"id": "5", "type": "private"}, "from": {}, "text": "/start"}}):
            self.assertEqual(self.hook(junk).status_code, 200)
        self.assertEqual(TgChat.objects.count(), 0)
        self.assertEqual(self.client.post("/api/telegram/", data="{не json", content_type="application/json",
                                          headers={"X-Telegram-Bot-Api-Secret-Token": self.secret}).status_code, 200)
        for _ in range(telegram.MESSAGES_PER_MINUTE + 5):
            self.say("/start")
        self.assertEqual(len(self.texts()), telegram.MESSAGES_PER_MINUTE)
        self.say("/start <b>x</b> ../../etc", chat=556)           # неподходящий промокод отбрасывается
        self.assertEqual(TgChat.objects.get(chat_id=556).data, {"promo": ""})

    def test_student_is_connected_to_the_bot_by_a_personal_link(self):
        from backend.models import TgChat
        TgChat.objects.create(chat_id=901, user=self.mentor)
        TgChat.objects.create(chat_id=902, user=self.admin)
        store.system_write("students", "s1", student("t1", name="Иванов Пётр", telegram=""))
        post = lambda sid: self.client.post("/api/telegram/student-link/", data=json.dumps({"studentId": sid}), content_type="application/json")
        self.login("blogger")
        self.assertEqual(post("s1").status_code, 403)
        self.login("other")
        self.assertEqual(post("s1").status_code, 403)                       # чужого студента подключить нельзя
        self.login("mentor")
        self.assertEqual(post("s2").status_code, 403)
        self.assertEqual(post("nope").status_code, 403)
        url = post("s1").json()["url"]
        self.assertTrue(url.startswith("https://t.me/menti_bot?start=stu_"))
        code = url.split("start=")[1]
        self.say("/start stu_wrong", chat=710)
        self.assertIn("устарела", self.texts(710)[-1])
        self.say("/start " + code, chat=901)                                # сотрудник случайно открыл ссылку сам
        self.assertNotIn("tgId", Doc.objects.get(doc_id="s1").data)
        self.say("/start " + code, chat=711, username="petr_i")
        doc = Doc.objects.get(doc_id="s1").data
        self.assertEqual((doc["tgId"], doc["telegram"]), (711, "@petr_i"))
        self.assertEqual((TgChat.objects.get(chat_id=711).student_id, TgChat.objects.get(chat_id=711).state), ("s1", "done"))
        self.assertIn("напомин", self.texts(711)[-1])
        self.say("/start " + code, chat=712)                                # ссылка одноразовая
        self.assertEqual(Doc.objects.get(doc_id="s1").data["tgId"], 711)
        self.assertIn("устарела", self.texts(712)[-1])
        self.sent.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.say("Оплатил вчера, +7 900 111-22-33", chat=711, username="petr_i")
        self.assertIn("Оплатил вчера", Doc.objects.get(doc_id="s1").data["notes"][-1]["text"])
        notice = self.texts(901)
        self.assertEqual(len(notice), 1)                                    # ментор узнаёт о сообщении студента
        self.assertIn("Иванов Пётр", notice[0])
        self.assertNotIn("111-22", notice[0])                               # сам текст в Telegram не дублируется
        self.assertEqual(self.texts(902), [])                               # у студента есть ментор: администратору не шлём
        self.say("И ещё вопрос", chat=711, username="petr_i")
        self.assertEqual(len(self.texts(901)), 1)                           # не чаще раза в десять минут
        url2 = post("s1").json()["url"]                                     # студент сменил аккаунт: новая ссылка перепривязывает чат
        self.say("/start " + url2.split("start=")[1], chat=713)
        self.assertEqual(Doc.objects.get(doc_id="s1").data["tgId"], 713)
        self.assertEqual(TgChat.objects.get(chat_id=711).student_id, "")
        self.assertIn("в другой чат", self.texts(711)[-1])                   # прежний чат узнаёт, что отключён
        self.assertEqual(post("s1").json()["url"], post("s1").json()["url"])  # неиспользованная ссылка выдаётся та же
        store.system_write("students", "s5", student("t1", name="Другой Студент"))
        self.say("/start " + post("s5").json()["url"].split("start=")[1], chat=713)   # тот же чат подключили к другой карточке
        self.assertNotIn("tgId", Doc.objects.get(doc_id="s1").data)          # в прежней карточке отметка о чате снята
        self.assertEqual(Doc.objects.get(doc_id="s5").data["tgId"], 713)

    def test_staff_links_telegram_and_gets_assignment_notice(self):
        from backend.models import TgChat
        self.login("blogger")
        self.assertEqual(self.client.post("/api/telegram/link/").status_code, 403)
        self.login("mentor")
        me = self.client.get("/api/me/").json()["telegram"]
        self.assertEqual(me, {"enabled": True, "bot": "menti_bot", "linked": False, "backup": False})
        url = self.client.post("/api/telegram/link/").json()["url"]
        self.assertTrue(url.startswith("https://t.me/menti_bot?start=link_"))
        code = url.split("start=")[1]
        self.say("/start link_wrong", chat=700)
        self.assertIn("устарела", self.texts(700)[-1])
        self.say("/start " + code, chat=700)
        self.assertIn("Уведомления CRM", self.texts(700)[-1])
        self.assertEqual(TgChat.objects.get(chat_id=700).user, self.mentor)
        self.say("/start " + code, chat=701)                       # ссылка одноразовая
        self.assertIn("устарела", self.texts(701)[-1])
        self.assertTrue(self.client.get("/api/me/").json()["telegram"]["linked"])
        self.say("привет", chat=700)                               # сотрудник не становится заявкой
        self.assertFalse(self.lead())
        # администратор закрепляет студента за ментором: ментору приходит сообщение
        self.login("admin")
        with self.captureOnCommitCallbacks(execute=True):
            self.send("patch", "students", "s2", {"mentorId": "t1"})
        self.assertIn("За вами закреплён студент: Чужой Студент", self.texts(700)[-1])
        # сам себе ментор уведомление не шлёт
        before = len(self.texts(700))
        self.login("mentor")
        with self.captureOnCommitCallbacks(execute=True):
            self.send("put", "students", "own1", student("t1"))
        self.assertEqual(len(self.texts(700)), before)
        self.assertEqual(self.client.delete("/api/telegram/link/").json(), {"linked": False})
        self.assertIsNone(TgChat.objects.get(chat_id=700).user)

    def test_stranger_cannot_write_into_someone_elses_card(self):
        from backend.models import TgChat
        store.system_write("students", "s8", student("t1", name="Жертва Анна", phone="+7 999 000-11-22"))
        self.say("/start", username=None); self.press(username=None); self.say("Посторонний", username=None)
        self.say("8 999 000 11 22", username=None)                 # чужой номер, набранный текстом
        self.say("оплатил, реквизиты новые", username=None)
        notes = Doc.objects.get(doc_id="s8").data["notes"]
        self.assertEqual(len(notes), 1)                             # одна пометка о повторной заявке
        self.assertEqual(TgChat.objects.get(chat_id=555).student_id, "")
        self.assertNotIn("tgId", Doc.objects.get(doc_id="s8").data)
        for text in ("ещё сообщение", "и ещё", "/start"):
            self.say(text, username=None)
        self.assertEqual(len(Doc.objects.get(doc_id="s8").data["notes"]), 1)   # дальше в чужую карточку ничего не пишется
        self.assertIn("уже у ментора", self.texts()[-1])

    def test_global_lead_cap_and_start_parsing(self):
        from backend import telegram
        from backend.models import TgChat
        with mock_limits(telegram, 2):
            for chat in (601, 602, 603):
                self.say("/start", chat=chat, username=f"user{chat}"); self.press(chat=chat, username=f"user{chat}")
                self.say("Имя Фамилия", chat=chat, username=f"user{chat}"); self.say("цель", chat=chat, username=f"user{chat}")
        self.assertEqual(Doc.objects.filter(collection="students", data__source="telegram").count(), 2)
        self.assertIn("много заявок", self.texts(603)[-1])
        self.assertEqual(TgChat.objects.get(chat_id=603).state, "goal")       # человек сможет повторить позже
        self.say("/startBLOG", chat=610)                                         # не команда /start: промокод из неё не берётся
        self.say("/start@menti_bot BLOG", chat=611)
        self.assertEqual(TgChat.objects.get(chat_id=611).data, {"promo": "BLOG"})
        self.assertEqual(TgChat.objects.get(chat_id=610).data, {"promo": ""})

    def test_deactivated_or_partner_account_loses_telegram_link(self):
        from backend.models import TgChat
        TgChat.objects.create(chat_id=701, user=self.mentor)
        self.login("admin")
        self.client.patch(f"/api/accounts/{self.mentor.pk}/", data=json.dumps({"active": False}), content_type="application/json")
        self.assertIsNone(TgChat.objects.get(chat_id=701).user)
        self.client.patch(f"/api/accounts/{self.mentor.pk}/", data=json.dumps({"active": True}), content_type="application/json")
        with self.captureOnCommitCallbacks(execute=True):
            self.send("patch", "students", "s2", {"mentorId": "t1"})
        self.assertEqual(self.texts(701), [])                                    # после возврата доступа уведомления сами не вернулись

    def test_purged_student_chat_is_forgotten(self):
        from backend.models import TgChat
        self.say("/start"); self.press(); self.say("Вера Сайтова"); self.say("цель")
        lead = self.lead()
        self.assertTrue(TgChat.objects.filter(student_id=lead.doc_id).exists())
        store.delete(store.Access(self.admin), "students", lead.doc_id)
        self.assertFalse(TgChat.objects.filter(chat_id=555).exists())

    def test_notification_text_cannot_carry_links(self):
        from backend.models import TgChat
        TgChat.objects.create(chat_id=900, user=self.admin)
        self.say("/start"); self.press(); self.say("Сессия истекла\nвойдите на https://evil.example/login"); self.say("цель")
        note = self.texts(900)[-1]
        self.assertNotIn("evil.example", note)
        self.assertNotIn("https://evil", note)
        self.assertEqual(note.count("\n"), 1)                                   # единственный перенос — перед адресом CRM

    def test_site_lead_notifies_admins(self):
        from backend.models import TgChat
        TgChat.objects.create(chat_id=900, user=self.admin)
        with override_settings(LEAD_WEBHOOK_TOKEN="secret-token-0123456789"), self.captureOnCommitCallbacks(execute=True):
            self.client.post("/api/leads/?token=secret-token-0123456789", {"name": "Мария Соколова", "phone": "+7 900 111-22-33"})
        self.assertIn("Новая заявка с сайта: Мария Соколова", self.texts(900)[-1])
        self.assertNotIn("+7 900", self.texts(900)[-1])            # телефон в Telegram не уходит

    def test_telegram_failure_does_not_break_writes(self):
        from unittest import mock
        from backend import telegram
        from backend.models import TgChat
        TgChat.objects.create(chat_id=900, user=self.admin)
        with mock.patch.object(telegram, "send", side_effect=OSError("сеть")), self.assertLogs("backend.telegram", level="ERROR"):
            res = self.say("/start")
        self.assertEqual(res.status_code, 200)

    def test_unreachable_telegram_is_not_retried_for_a_while(self):
        from unittest import mock
        import urllib.error
        from backend import telegram
        self.addCleanup(cache.clear)
        mock.patch.stopall()                                                     # здесь нужен настоящий call
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("нет сети")) as opened, \
                self.assertLogs("backend.telegram", level="WARNING"):
            self.assertIsNone(telegram.call("getMe"))
            self.assertIsNone(telegram.call("getMe"))
            self.assertIsNone(telegram.send(1, "x"))
        self.assertEqual(opened.call_count, 1)                                   # после сбоя минуту не стучимся


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
    def test_import_restores_directions_and_expenses(self):
        raw = sample_backup()
        raw["version"] = 6
        raw["data"]["directions"] = [{"id": "bd1", "name": "Аналитик 1С", "terms": [{"from": "2000-01", "parties": [{"kind": "partner", "id": "bp1", "share": 40}]}]}]
        raw["data"]["expenses"] = [{"id": "be1", "directionId": "bd1", "date": "2026-08-10", "amount": 10000, "comment": "Реклама"}]
        raw["data"]["cohorts"][0]["directionId"] = "bd1"
        raw["data"]["directions"][0]["demo"] = True                         # пример направления нужен настоящему потоку: загружается
        raw["data"]["directions"].append({"id": "bd2", "name": "Ненужный пример", "demo": True})
        import_backup(raw)
        self.assertEqual(Doc.objects.get(collection="directions", doc_id="bd1").data["terms"][0]["parties"][0]["share"], 40)
        self.assertFalse(Doc.objects.filter(collection="directions", doc_id="bd2").exists())
        self.assertEqual(Doc.objects.get(collection="expenses", doc_id="be1").data["amount"], 10000)
        from backend import reports
        report = reports.build("bp1", reports.load())
        self.assertEqual([(m["received"], m["expenses"], m["accrued"]) for m in report["months"]], [(50000, 10000, 16000)])

    def test_import_keeps_mentor_payouts(self):
        raw = sample_backup()
        team_id = raw["data"]["team"][0]["id"]
        raw["data"]["payouts"] = list(raw["data"].get("payouts") or []) + [
            {"id": "mpA", "mentorId": team_id, "amount": 3000, "date": "2026-02-01"},
            {"id": "mpB", "mentorId": "нет-такого", "amount": 1, "date": "2026-02-01"}]
        import_backup(raw)
        self.assertTrue(Doc.objects.filter(collection="payouts", doc_id="mpA").exists())
        self.assertFalse(Doc.objects.filter(collection="payouts", doc_id="mpB").exists())

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


@override_settings(TELEGRAM_BOT_TOKEN="123456:test-token", PUBLIC_URL="https://crm.example.test", BACKUP_PASSPHRASE="dlinnyi-parol-kopii")
class SchedulerTests(BaseCase):
    """Планировщик: утренняя сводка, напоминание о встрече и зашифрованная копия базы в Telegram администратору."""

    def setUp(self):
        import datetime
        from unittest import mock
        from backend import telegram
        from backend.models import TgChat
        cache.clear()
        self.addCleanup(cache.clear)
        self.sent, self.files = [], []
        for name, fake in (("send", lambda chat_id, text, **kw: self.sent.append((chat_id, text)) or {"ok": True}),
                           ("send_document", lambda chat_id, filename, content, caption="": self.files.append((chat_id, filename, content, caption)) or {"ok": True})):
            patcher = mock.patch.object(telegram, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        TgChat.objects.create(chat_id=100, user=self.admin)
        TgChat.objects.create(chat_id=200, user=self.mentor)
        TgChat.objects.create(chat_id=300, user=self.blogger)
        self.day = datetime.date(2026, 10, 6)
        store.system_write("students", "s1", student("t1", name="Иванов Пётр", phone="+7 900 000-00-01", stage="studying",
                                                     next={"text": "Проверить домашку", "date": "2026-10-06"},
                                                     payments=[{"id": "a", "amount": 30000, "due": "2026-10-01", "paid": None},
                                                               {"id": "b", "amount": 20000, "due": "2026-10-06", "paid": None},
                                                               {"id": "c", "amount": 10000, "due": "2026-09-01", "paid": "2026-09-01"}]))
        store.system_write("students", "s2", student("t2", name="Чужой Студент", next={"text": "Связаться", "date": "2026-10-03"}))
        store.system_write("students", "s3", student(None, name="Новая Заявка"))
        store.system_write("students", "s4", student("t1", name="В Корзине", deletedAt="2026-10-01T00:00:00Z", next={"text": "x", "date": "2026-10-01"}))
        store.system_write("students", "s5", student("t1", name="Выбыл", stage="lost", next={"text": "x", "date": "2026-10-01"},
                                                     payments=[{"amount": 5000, "due": "2026-10-01", "paid": None}]))

    def at(self, hour, minute=0, day=None):
        import datetime
        from django.utils import timezone
        return timezone.make_aware(datetime.datetime.combine(day or self.day, datetime.time(hour, minute)))

    def run_at(self, *args, **kwargs):
        from backend import scheduler
        scheduler.run(self.at(*args, **kwargs))

    def texts(self, chat):
        return [text for chat_id, text in self.sent if chat_id == chat]

    def test_morning_digest_goes_once_a_day_to_each_employee_with_their_own_numbers(self):
        store.system_write("meetings", "m7", {"title": "Разбор", "date": "2026-10-06", "time": "18:00", "studentId": "s1", "mentorId": "t1", "status": "planned"})
        self.run_at(8, 59)
        self.assertEqual(self.sent, [])
        self.run_at(9, 1)
        admin, mentor = self.texts(100), self.texts(200)
        self.assertEqual((len(admin), len(mentor), self.texts(300)), (1, 1, []))          # партнёру сводка не идёт
        self.assertIn("шагов: 2 (просрочено 1)", admin[0])
        self.assertIn("просрочено платежей: 1 на 30 000 ₽", admin[0])
        self.assertIn("срок оплаты сегодня: 1", admin[0])
        self.assertIn("заявок без ментора: 1", admin[0])
        self.assertIn("встреч: 1", admin[0])
        self.assertIn("шагов: 1", mentor[0])
        self.assertNotIn("просрочено 1)", mentor[0])                                      # чужой просроченный шаг ментору не считается
        self.assertNotIn("без ментора", mentor[0])
        for text in admin + mentor:
            self.assertIn("https://crm.example.test/", text)
            for secret in ("+7 900", "Иванов", "Чужой"):
                self.assertNotIn(secret, text)                                            # в сводке только числа
        self.run_at(9, 2)
        self.run_at(11, 30)
        self.assertEqual(len(self.sent), 2)                                               # второй раз за день не приходит
        import datetime
        self.run_at(9, 5, day=self.day + datetime.timedelta(days=1))
        self.assertEqual(len(self.texts(100)), 2)

    def test_no_digest_in_the_evening_or_when_there_is_nothing_to_do(self):
        self.run_at(15, 0)
        self.assertEqual(self.sent, [])                                                   # днём «утренняя» сводка уже не нужна
        Doc.objects.filter(collection="students").delete()
        self.run_at(9, 1)
        self.assertEqual(self.sent, [])

    def test_meeting_reminder_goes_to_the_one_who_runs_it(self):
        store.system_write("meetings", "m12", {"title": "Кривая", "date": "2026-10-06", "time": "25:99", "studentId": ["s1"], "mentorId": {"a": 1}, "status": "planned"})
        store.system_write("meetings", "m7", {"title": "Разбор https://evil.example +7 900 123-45-67", "date": "2026-10-06", "time": "15:00", "studentId": "s1", "mentorId": "t1", "status": "planned"})
        store.system_write("meetings", "m8", {"title": "Созвон-знакомство", "date": "2026-10-06", "time": "15:10", "studentId": "s3", "mentorId": None, "status": "planned"})
        store.system_write("meetings", "m9", {"title": "Уже прошла", "date": "2026-10-06", "time": "15:05", "mentorId": "t1", "status": "done"})
        store.system_write("meetings", "m10", {"title": "Через два часа", "date": "2026-10-06", "time": "16:30", "mentorId": "t1", "status": "planned"})
        store.system_write("meetings", "m11", {"title": "Без времени", "date": "2026-10-06", "time": "", "mentorId": "t1", "status": "planned"})
        self.run_at(14, 15)
        self.assertEqual(len(self.texts(200)), 1)
        self.assertIn("15:00", self.texts(200)[0])
        self.assertIn("Разбор", self.texts(200)[0])
        self.assertNotIn("evil.example", self.texts(200)[0])
        self.assertNotIn("123-45", self.texts(200)[0])                                    # телефон из названия не уходит
        self.assertEqual(len(self.texts(100)), 1)                                         # встреча без ведущего — администратору
        self.assertIn("Созвон-знакомство", self.texts(100)[0])
        self.run_at(14, 16)
        self.run_at(14, 50)
        self.assertEqual(len(self.sent), 2)                                               # об одной встрече — один раз
        self.run_at(15, 1)
        self.assertEqual(len(self.sent), 2)                                               # начавшиеся не напоминаются
        import datetime
        tomorrow = self.day + datetime.timedelta(days=1)
        store.system_write("meetings", "m13", {"title": "Кривая завтра", "date": tomorrow.isoformat(), "time": "ab:cd", "studentId": ["s1"], "mentorId": {"a": 1}, "status": "planned"})
        self.run_at(9, 1, day=tomorrow)                                                   # кривая встреча не ломает сводку
        self.assertEqual(len([x for x in self.texts(200) if "Доброе утро" in x]), 1)

    def test_encrypted_backup_goes_to_admin_and_can_be_restored(self):
        from backend import backup
        self.run_at(3, 59)
        self.assertEqual(self.files, [])
        self.run_at(4, 5)
        self.assertEqual([chat for chat, *_ in self.files], [100])                        # только администратору
        chat, filename, content, caption = self.files[0]
        self.assertTrue(filename.endswith(".crmbackup"))
        for plain in ("Иванов".encode(), b"+7 900", b"students"):
            self.assertNotIn(plain, content)                                              # в файле нет ничего читаемого
        self.run_at(4, 6)
        self.run_at(20, 0)
        self.assertEqual(len(self.files), 1)                                              # раз в сутки
        with self.assertRaises(backup.BackupError):
            backup.parse_backup(content)                                                  # без пароля не открывается
        with self.assertRaises(backup.BackupError):
            backup.parse_backup(content, passphrase="ne-tot-parol-2026")
        data = backup.parse_backup(content, passphrase="dlinnyi-parol-kopii")["data"]
        self.assertEqual(sorted(s["id"] for s in data["students"]), ["s1", "s2", "s3", "s4", "s5"])   # с корзиной
        self.assertEqual([p["id"] for p in data["partners"]], ["p1"])
        Doc.objects.filter(collection__in=("students", "partners", "team", "cohorts")).delete()
        self.login("admin")
        from django.core.files.uploadedfile import SimpleUploadedFile
        res = self.client.post("/backup/import/", {"file": SimpleUploadedFile("k.crmbackup", content), "passphrase": "ne-tot-parol-2026"})
        self.assertContains(res, "не подошёл")
        self.assertFalse(Doc.objects.filter(collection="students").exists())
        res = self.client.post("/backup/import/", {"file": SimpleUploadedFile("k.crmbackup", content), "passphrase": "dlinnyi-parol-kopii"})
        self.assertContains(res, "Готово")
        self.assertEqual(Doc.objects.get(collection="students", doc_id="s1").data["phone"], "+7 900 000-00-01")
        self.assertTrue(Doc.objects.get(collection="students", doc_id="s4").data["deletedAt"])

    def test_backup_is_not_sent_without_a_good_passphrase_and_retries_after_failure(self):
        from unittest import mock
        from backend import telegram
        with override_settings(BACKUP_PASSPHRASE="korotkii"):
            self.run_at(5, 0)
        self.assertEqual(self.files, [])                                                  # незашифрованная база в Telegram не уходит
        with mock.patch.object(telegram, "send_document", return_value=None):             # Telegram недоступен
            self.run_at(5, 1)
        self.run_at(5, 2)
        self.assertEqual(self.files, [])                                                  # сразу не повторяем
        self.run_at(6, 5)
        self.assertEqual(len(self.files), 1)                                              # через час — ещё попытка

    def test_document_upload_request_and_me_flag(self):
        from unittest import mock
        from backend import telegram
        mock.patch.stopall()                                                              # здесь нужна настоящая отправка
        seen = {}

        class Reply:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return b'{"ok": true, "result": {"message_id": 7}}'

        def fake(request, timeout):
            seen.update(url=request.full_url, body=request.data, type=request.get_header("Content-type"), timeout=timeout)
            return Reply()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            result = telegram.send_document(100, "копия 2026.crmbackup", b"\x00\x01binary", "Подпись")
        self.assertEqual(result, {"message_id": 7})
        self.assertTrue(seen["url"].endswith("/sendDocument"))
        self.assertIn("multipart/form-data; boundary=", seen["type"])
        self.assertIn(b'name="chat_id"\r\n\r\n100\r\n', seen["body"])
        self.assertIn(b"\r\n\r\n\x00\x01binary\r\n--", seen["body"])
        self.assertIn(b'filename="______2026.crmbackup"', seen["body"])                     # имя файла без пробелов и кириллицы
        with mock.patch("urllib.request.urlopen", side_effect=OSError("нет сети")), self.assertLogs("backend.telegram", level="WARNING"):
            self.assertIsNone(telegram.send_document(100, "a", b"x"))
        cache.clear()
        self.login("admin")
        self.assertTrue(self.client.get("/api/me/").json()["telegram"]["backup"])
        self.login("mentor")
        self.assertFalse(self.client.get("/api/me/").json()["telegram"]["backup"])
        with override_settings(BACKUP_PASSPHRASE=""):
            self.login("admin")
            self.assertFalse(self.client.get("/api/me/").json()["telegram"]["backup"])

    def test_student_gets_a_payment_reminder_on_the_due_day(self):
        from backend.models import TgChat
        TgChat.objects.create(chat_id=777, student_id="s1", state="done")
        TgChat.objects.create(chat_id=778, student_id="s9", state="done")          # чат привязан к другой карточке
        pay = lambda sid, tg, **extra: store.system_write("students", sid, student("t1", tgId=tg, stage="offer", payments=[
            {"id": "a", "amount": 15000, "due": "2026-10-06", "paid": None, "comment": "С зарплаты 2/6"},
            {"id": "b", "amount": 5000, "due": "2026-10-06", "paid": None},
            {"id": "c", "amount": 15000, "due": "2026-10-06", "paid": "2026-10-05"},
            {"id": "d", "amount": 15000, "due": "2026-11-06", "paid": None}], **extra))
        pay("s1", 777)
        pay("s6", 778)                                                              # tgId указывает на чужой чат: не пишем
        pay("s7", 777, deletedAt="2026-10-01T00:00:00Z")
        pay("s8", [777])
        self.run_at(9, 59)
        self.assertEqual(self.texts(777), [])
        self.run_at(10, 1)
        self.assertEqual(len(self.texts(777)), 1)
        self.assertIn("20 000 ₽", self.texts(777)[0])                               # два неоплаченных платежа с сегодняшним сроком
        self.assertEqual(self.texts(778), [])
        self.run_at(10, 2)
        self.run_at(18, 0)
        self.assertEqual(len(self.texts(777)), 1)                                   # раз в день
        from unittest import mock
        from backend import telegram
        TgChat.objects.create(chat_id=779, student_id="s10", state="done")
        pay("s10", 779)
        with mock.patch.object(telegram, "send", return_value=None):                # Telegram недоступен
            self.run_at(18, 5)
        self.run_at(18, 6)
        self.assertEqual(len(self.texts(779)), 1)                                   # напоминание ушло при следующем проходе
        import datetime
        self.run_at(10, 5, day=self.day + datetime.timedelta(days=1))
        self.assertEqual(len(self.texts(777)), 1)                                   # на следующий день срока нет — тишина
        self.sent.clear()
        store.system_write("students", "s1", student("t1", tgId=777, stage="lost", payments=[{"id": "a", "amount": 1, "due": "2026-11-06", "paid": None}]))
        self.run_at(10, 5, day=datetime.date(2026, 11, 6))
        self.assertEqual(self.texts(777), [])                                       # выбывшему не напоминаем
        self.run_at(22, 0, day=datetime.date(2026, 11, 6))
        self.assertEqual(self.texts(777), [])

    def test_scheduler_is_silent_without_the_bot(self):
        with override_settings(TELEGRAM_BOT_TOKEN=""):
            self.run_at(9, 1)
        self.assertEqual((self.sent, self.files), ([], []))
