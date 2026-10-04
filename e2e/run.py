"""Сквозная проверка CRM в браузере на настоящем сервере.

Скрипт сам поднимает сервер на пустой временной базе, входит администратором, проходит основные
сценарии страницы и проверяет роли: ментор видит только своих студентов, партнёр — только отчёт.

Запуск из корня репозитория:  python e2e/run.py
Нужны: pip install playwright, браузер Chromium (CHROME=/путь/к/chrome, если он не в стандартном месте).
"""
import datetime, json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(__file__).resolve().parent / "shots"; SHOTS.mkdir(exist_ok=True)
PORT = int(os.environ.get("E2E_PORT", "8793")); URL = f"http://127.0.0.1:{PORT}/"
PASSWORD = "e2e-parol-2026"; TOKEN = "e2e-lead-token-0123456789"
errs = []; ok = []; bad = []
def check(name, cond, info=""):
    (ok if cond else bad).append(name); print(("PASS " if cond else "FAIL ") + name, info if not cond else "")
today = datetime.date.today().isoformat(); tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

tmp = tempfile.mkdtemp(prefix="crm-e2e-")
env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/e2e.sqlite3", "LEAD_WEBHOOK_TOKEN": TOKEN, "CRM_ADMIN_PASSWORD": PASSWORD, "DEBUG": "1",
       "TELEGRAM_BOT_TOKEN": "1:e2e-not-a-real-token", "SECRET_KEY": "e2e-secret-key-not-for-production-0123456789"}
def manage(*args):
    res = subprocess.run([sys.executable, "manage.py", *args], cwd=ROOT, env=env, capture_output=True, text=True)
    if res.returncode: sys.exit(f"manage.py {' '.join(args)} завершилась с ошибкой:\n{res.stderr[-2000:]}")
manage("migrate", "-v0"); manage("create_admin", "admin", "--name", "Анна Владелец")
# Бот «подключён» без обращения к Telegram: имя бота записывается напрямую, сообщения бота уходят в никуда
manage("shell", "-c", "from backend import store; s = store.state(); s.bot_username = 'menti_e2e_bot'; s.save()")
TG_SECRET = __import__("hmac").new(env["SECRET_KEY"].encode(), b"telegram-webhook", "sha256").hexdigest()[:48]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    opener.open(URL + "login/", timeout=1); sys.exit(f"Порт {PORT} уже занят другим сервером: остановите его или задайте E2E_PORT")
except OSError: pass
log = open(Path(tmp) / "server.log", "w")
server = subprocess.Popen([sys.executable, "manage.py", "runserver", f"127.0.0.1:{PORT}", "--noreload"], cwd=ROOT, env=env, stdout=log, stderr=log)
for _ in range(75):
    if server.poll() is not None: sys.exit("Сервер не запустился:\n" + open(Path(tmp) / "server.log").read()[-2000:])
    try: opener.open(URL + "login/", timeout=1); break
    except OSError: time.sleep(0.2)

# Доступ к данным страницы и перехват скачиваемых файлов — только для проверок.
# Проверяется содержимое файла, а не само скачивание браузером: его стоит раз проверить руками.
INIT = """
Object.defineProperty(window, '__store', { get: () => window.crmServer.__cache() });
const mk = URL.createObjectURL.bind(URL);
URL.createObjectURL = b => { b.text().then(t => { window.__saved = { data: t }; }); return mk(b); };
const click = HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click = function () { if (!this.download) click.call(this); };
"""
def login(page, username, password=PASSWORD):
    page.goto(URL + "login/"); page.fill("input[name=username]", username); page.fill("input[name=password]", password); page.click("button")
def shot(page, name, **kw): page.screenshot(path=str(SHOTS / name), **kw)
def tg(update):
    req = urllib.request.Request(URL + "api/telegram/", data=json.dumps(update).encode(), headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": TG_SECRET})
    return opener.open(req, timeout=60).status
def tg_say(text, chat=4242):
    return tg({"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat, "type": "private"}, "from": {"id": chat, "is_bot": False, "first_name": "Глеб", "username": "gleb_tg"}, "text": text}})
def post(path, data):
    req = urllib.request.Request(URL + path, data=json.dumps(data).encode(), headers={"Content-Type": "application/json", "X-Token": TOKEN})
    return json.loads(opener.open(req, timeout=5).read())

try:
  with sync_playwright() as p:
      b = p.chromium.launch(**({"executable_path": os.environ["CHROME"]} if os.environ.get("CHROME") else {}))
      ctx = b.new_context(viewport={"width": 1440, "height": 900}); ctx.add_init_script(INIT)
      ctx.grant_permissions(["clipboard-read", "clipboard-write"])
      pg = ctx.new_page()
      pg.on("pageerror", lambda e: errs.append("PAGEERROR " + str(e)))
      pg.on("console", lambda m: m.type == "error" and "ERR_TUNNEL" not in m.text and "404" not in m.text and "400 (Bad Request)" not in m.text and errs.append(m.text))  # 400 — ожидаемые отказы в проверках формы доступа
      store = lambda: pg.evaluate("window.__store")
      studs = lambda: {k: v for k, v in store().items() if k.startswith("students/")}
      login(pg, "admin")
      pg.wait_for_selector("text=CRM готова к работе"); shot(pg, "s0_welcome.png")
      pg.click("text=Посмотреть на примере"); pg.wait_for_function("() => !S.busy && D().students.length >= 30 && D().meetings.length >= 8", timeout=60000); pg.wait_for_selector(".today"); pg.wait_for_timeout(400)
      shot(pg, "s1_today.png", full_page=True)
      check("today panels", pg.locator(".panel").count() >= 3)
      check("nav counter", pg.locator(".nav .n").count() >= 1)
      check("logged in as admin, server mode", pg.evaluate("S.server && S.isOwner && !S.limited && S.me.name === 'Анна Владелец'"))
      pg.locator(".today [data-act=pay-remind]").first.click(); pg.wait_for_timeout(300)
      clip = pg.evaluate("navigator.clipboard.readText()")
      check("payment reminder copied", "Напоминаю про платёж" in clip and "₽" in clip and "добрый день" in clip.lower(), clip)
      print("   reminder:", clip)
      # шаг выполнен -> что дальше
      row = pg.locator(".today .rows li.click").first; sid = row.get_attribute("data-id"); old = store()["students/" + sid]["next"]["text"]
      row.locator(".chk").click(); pg.wait_for_selector("#mform")
      check("after-step modal", "Что дальше" in pg.inner_text("#mform h2"))
      pg.fill("#m-text", "Созвон"); pg.click("#mform .chip >> text=Завтра"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      s = store()["students/" + sid]
      check("next step saved", s["next"] == {"text": "Созвон", "date": tomorrow}, s.get("next"))
      check("step logged in notes", s["notes"][-1]["text"] == "Сделано: " + old and s["notes"][-1].get("kind") == "step")
      # отмена выполнения
      row = pg.locator(".today .rows li.click").first; sid2 = row.get_attribute("data-id"); before = store()["students/" + sid2]
      row.locator(".chk").click(); pg.wait_for_selector("#mform"); pg.click("#mform >> text=Без шага"); pg.wait_for_timeout(200)
      check("step cleared", store()["students/" + sid2]["next"] is None)
      pg.click("#toast button"); pg.wait_for_timeout(300)
      after = store()["students/" + sid2]
      check("undo restores step", after["next"] == before["next"] and len(after["notes"]) == len(before["notes"]))
      # быстрая заявка
      n0 = len(studs())
      pg.click(".page-head [data-act=quick]"); pg.fill("#m-name", "Тестов Иван"); pg.fill("#m-contact", "@ivan_test"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      new = [v for v in studs().values() if v["name"] == "Тестов Иван"]
      check("quick add", len(studs()) == n0 + 1 and new[0]["telegram"] == "@ivan_test" and new[0]["next"] == {"text": "Связаться", "date": today} and new[0]["stage"] == "new", new)
      pg.click(".page-head [data-act=quick]"); pg.fill("#m-name", "Дубль"); pg.fill("#m-contact", "ivan_test"); pg.click("#m-submit"); pg.wait_for_timeout(200)
      check("duplicate blocked", "Уже есть в базе" in pg.inner_text("#m-err") and len(studs()) == n0 + 1)
      pg.fill("#m-contact", "+7 999 123-45-67"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      check("phone parsed", any(v["name"] == "Дубль" and v["phone"] == "+7 999 123-45-67" and v["telegram"] == "" for v in studs().values()))
      pg.click(".page-head [data-act=quick]"); pg.fill("#m-name", "Дубль 2"); pg.fill("#m-contact", "8 (999) 123 45 67"); pg.click("#m-submit"); pg.wait_for_timeout(200)
      check("8/+7 duplicate", "Уже есть в базе: Дубль" in pg.inner_text("#m-err")); pg.keyboard.press("Escape")
      pg.click(".page-head [data-act=quick]"); pg.fill("#m-name", "Полная Форма"); pg.fill("#m-contact", "a@b.ru"); pg.click("[data-act=m-link]"); pg.wait_for_timeout(200)
      check("all-fields prefill", pg.input_value("#m-name") == "Полная Форма" and pg.input_value("#m-email") == "a@b.ru")
      pg.keyboard.press("Escape")
      # доска
      pg.wait_for_timeout(200)
      check("no backup banner on server", pg.locator("text=Резервной копии ещё нет").count() == 0)
      pg.click(".nav >> text=Воронка"); pg.wait_for_selector("#board")
      check("sales pipeline cols", pg.locator(".col").count() == 4, pg.locator(".col").count())
      shot(pg, "s2_board.png")
      tile = pg.locator(".col[data-stage=new] .tile").first; tid = tile.get_attribute("data-id"); hist0 = len(store()["students/" + tid]["history"])
      tile.drag_to(pg.locator(".col[data-stage=call] .col-body")); pg.wait_for_timeout(300)
      check("drag moves stage", store()["students/" + tid]["stage"] == "call")
      pg.click("#toast button"); pg.wait_for_timeout(300)
      st = store()["students/" + tid]
      check("undo stage", st["stage"] == "new" and len(st["history"]) == hist0, (st["stage"], len(st["history"])))
      pg.click(".seg >> text=Обучение"); pg.wait_for_timeout(150); check("study pipeline", pg.locator(".col").count() == 2)
      pg.click(".seg >> text=Все"); pg.wait_for_timeout(150); check("all pipeline", pg.locator(".col").count() == 9)
      pg.check("#f-lost"); pg.wait_for_timeout(150); check("lost column", pg.locator(".col").count() == 10); pg.uncheck("#f-lost")
      pg.click(".seg >> text=Обучение"); pg.wait_for_timeout(150)
      # панель студента
      t2 = pg.locator(".col[data-stage=studying] .tile").first; did = t2.get_attribute("data-id"); t2.click(); pg.wait_for_selector(".drawer")
      check("drawer open, board stays", pg.locator("#board").count() == 1 and pg.locator(".drawer h2").count() == 1)
      pg.wait_for_timeout(250); shot(pg, "s3_drawer.png")
      pg.fill(f"#note-{did}", "Заметка из панели"); pg.click(".drawer [data-act=note-add]"); pg.wait_for_timeout(300)
      check("note saved", store()["students/" + did]["notes"][-1]["text"] == "Заметка из панели")
      pg.select_option("#d-stage", "final_project"); pg.wait_for_timeout(300)
      check("stage via select", store()["students/" + did]["stage"] == "final_project")
      pg.click(".drawer [data-act=pay-plan]"); pg.fill("#m-total", "90000"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      pcount = len(store()["students/" + did]["payments"])
      pg.locator(".drawer [data-act=pay-done]").first.click(); pg.wait_for_timeout(300)
      paid1 = sum(1 for x in store()["students/" + did]["payments"] if x["paid"])
      pg.click("#toast button"); pg.wait_for_timeout(300)
      paid2 = sum(1 for x in store()["students/" + did]["payments"] if x["paid"])
      check("pay done + undo", paid1 == paid2 + 1 and pcount >= 3, (paid1, paid2, pcount))
      unpaid0 = sum(1 for x in store()["students/" + did]["payments"] if not x["paid"])
      pg.evaluate("""() => { const b = [...document.querySelectorAll('.drawer [data-act=pay-done]')]; b[0].click(); b[1].click(); }"""); pg.wait_for_timeout(500)
      unpaid1 = sum(1 for x in store()["students/" + did]["payments"] if not x["paid"])
      check("two fast pay-done both kept", unpaid1 == unpaid0 - 2, (unpaid0, unpaid1))
      pg.click(".drawer [data-act=step-edit]"); pg.wait_for_selector("#mform"); pg.fill("#m-text", "Проверить домашку"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      check("step from drawer", store()["students/" + did]["next"]["text"] == "Проверить домашку")
      pg.select_option("#d-stage", "lost"); pg.wait_for_selector("#mform"); pg.fill("#m-reason", "Дорого"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      st = store()["students/" + did]
      check("lost clears step", st["stage"] == "lost" and st["next"] is None and st["lostReason"] == "Дорого", (st["stage"], st["next"]))
      pg.click("#toast button"); pg.wait_for_timeout(300)
      st = store()["students/" + did]
      check("undo lost", st["stage"] == "final_project" and st["next"]["text"] == "Проверить домашку", (st["stage"], st["next"]))
      pg.keyboard.press("Escape"); pg.wait_for_timeout(150)
      check("esc closes drawer", pg.locator(".drawer").count() == 0)
      # поиск
      pg.keyboard.press("/"); pg.keyboard.type("Тестов"); pg.wait_for_timeout(150)
      check("search results", pg.locator("#gq-res button").count() == 1)
      pg.keyboard.press("Enter"); pg.wait_for_timeout(200)
      check("search opens drawer", "Тестов Иван" in pg.inner_text(".drawer h2") and pg.input_value("#gq") == "")
      shot(pg, "s4_drawer_lead.png"); pg.click("#d-x")
      # программа в панели студента
      pg.click(".nav >> text=Воронка"); pg.click(".seg >> text=Обучение"); pg.wait_for_timeout(200)
      check("progress on study tiles", pg.locator(".col[data-stage=studying] .tile .prog").count() >= 1)
      tl = pg.locator(".col[data-stage=studying] .tile").first; pid = tl.get_attribute("data-id"); tl.click(); pg.wait_for_selector(".drawer .mods")
      before = sum(1 for v in (store()["students/" + pid].get("progress") or {}).values() if v)
      box = pg.locator(".drawer .mods input:not(:checked)").first; mid = box.get_attribute("data-mod"); box.check(); pg.wait_for_timeout(300)
      pr = store()["students/" + pid]["progress"]
      check("module ticked, others kept", pr.get(mid) and sum(1 for v in pr.values() if v) == before + 1, pr)
      pg.locator(f".drawer .mods input[data-mod={mid}]").uncheck(); pg.wait_for_timeout(300)
      check("module unticked", not store()["students/" + pid]["progress"].get(mid))
      shot(pg, "s12_drawer_program.png"); pg.click("#d-x")
      # потоки
      pg.click(".nav >> text=Потоки"); pg.wait_for_selector(".cgrid"); pg.wait_for_timeout(200); shot(pg, "s13_cohorts.png")
      check("cohort cards", pg.locator(".ccard").count() == 3)
      pg.locator(".ccard", has_text="Поток 2").click(); pg.wait_for_selector(".mx"); pg.wait_for_timeout(200); shot(pg, "s14_cohort.png", full_page=True)
      cell = pg.locator(".mx:not(.on)").first; cid, cmid = cell.get_attribute("data-id"), cell.get_attribute("data-mid"); cell.click(); pg.wait_for_timeout(300)
      check("matrix cell toggles, no drawer", bool(store()["students/" + cid]["progress"].get(cmid)) and pg.locator(".drawer").count() == 0)
      boxes = pg.locator("tbody input[data-sel]"); boxes.nth(0).check(); pg.wait_for_timeout(100); pg.locator("tbody input[data-sel]").nth(1).check(); pg.wait_for_timeout(150)
      ids = pg.evaluate("[...document.querySelectorAll('tbody input[data-sel]:checked')].map(x => x.dataset.sel)")
      check("bulk bar shows count", "Выбрано: 2" in pg.inner_text(".bulk") and pg.locator(".drawer").count() == 0, ids)
      shot(pg, "s15_bulk.png")
      pg.click(".bulk >> text=Назначить шаг"); pg.fill("#m-text", "Проверить домашку по модулю 3"); pg.click("#mform .chip >> text=Через 3 дня"); pg.click("#m-submit"); pg.wait_for_timeout(500)
      # выбранные, но скрытые строки не затрагиваются
      pg.click(".nav >> text=Студенты"); pg.wait_for_timeout(150); pg.locator("thead input[data-sel-all]").check(); pg.wait_for_timeout(150)
      pg.fill("#f-q", "Тестов"); pg.wait_for_timeout(400)
      check("bulk bar counts visible only", "Выбрано: 1" in pg.inner_text(".bulk"), pg.inner_text(".bulk"))
      pg.click(".bulk >> text=Назначить шаг"); check("bulk modal counts visible only", "1 студент" in pg.inner_text("#mform h2"), pg.inner_text("#mform h2")); pg.keyboard.press("Escape")
      pg.click("[data-act=sel-clear]"); pg.click("[data-act=freset]"); pg.click(".nav >> text=Потоки"); pg.locator(".ccard", has_text="Поток 2").click(); pg.wait_for_selector(".mx")
      check("bulk step applied", all(store()["students/" + i]["next"]["text"] == "Проверить домашку по модулю 3" for i in ids) and pg.locator(".bulk").count() == 0)
      pg.locator("thead input[data-sel-all]").check(); pg.wait_for_timeout(150); nsel = pg.locator("tbody input[data-sel]:checked").count()
      check("select all", nsel == pg.locator("tbody input[data-sel]").count() and nsel > 2)
      pg.click(".bulk >> text=Ментор"); opt = pg.locator("#m-mentorId option").nth(1).get_attribute("value"); pg.select_option("#m-mentorId", opt); pg.click("#m-submit"); pg.wait_for_timeout(700)
      cohort_ids = [k for k, v in studs().items() if v.get("cohortId") == store()["students/" + cid]["cohortId"] and v["stage"] != "lost"]
      check("bulk mentor applied to all", all(store()[k]["mentorId"] == opt for k in cohort_ids), len(cohort_ids))
      pg.click("[data-act=cohort-edit]"); val = pg.input_value("#m-modules"); lines = val.split("\n")
      check("modules listed in form", len(lines) == 8)
      lines[0] = "Введение (переименован)"; pg.fill("#m-modules", "\n".join(lines)); pg.click("#m-submit"); pg.wait_for_timeout(400)
      pg.click("[data-act=cohort-edit]"); del lines[3]; pg.fill("#m-modules", "\n".join(lines) + "\nНовый модуль"); pg.click("#m-submit"); pg.wait_for_timeout(400)
      mods = [c for k, c in store().items() if k.startswith("cohorts/") and c["name"] == "Поток 2"][0]["modules"]
      check("module ids: rename keeps id, deleted id not reused", len(mods) == 8 and mods[0]["id"] == "m1" and mods[1]["id"] == "m2" and mods[3]["id"] == "m5" and not mods[7]["id"] in ("m4", "m1") and len(set(m["id"] for m in mods)) == 8, mods)
      # импорт списка
      pg.click(".nav >> text=Студенты"); pg.click("[data-act=import]"); pg.wait_for_selector("#m-rows")
      n1 = len(studs())
      pg.fill("#m-rows", "Имя\tТелефон\tTelegram\tИсточник\nИмпортов Пётр\t+7 911 000-00-01\t@import_petr\tBLOG1C\n\"Импортова, Анна\"; @import_anna\nТестов Иван\t\t@ivan_test\t\nПовтор\t+7 (911) 000 00 01\t\t\n\t\tsolo@example.com\t")
      pg.wait_for_timeout(300); txt = pg.inner_text("#m-preview"); btn = pg.inner_text("#m-submit")
      check("import preview", "Распознано строк: 5" in txt and "Новых: 3" in txt and "уже есть: Тестов Иван" in txt and "повтор в списке" in txt and btn == "Добавить: 3", (txt[:200], btn))
      shot(pg, "s16_import.png")
      pg.click("#m-submit"); pg.wait_for_timeout(900)
      imp = sorted([v for v in studs().values() if v["name"].startswith("Импортов")], key=lambda v: v["name"]) + [v for v in studs().values() if v["name"] == "solo@example.com"]
      petr = [v for v in imp if v["name"] == "Импортов Пётр"][0]
      check("import created leads", len(studs()) == n1 + 3 and len(imp) == 3 and petr["phone"] == "+7 911 000-00-01" and petr["telegram"] == "@import_petr" and petr["partnerShare"] == 40 and petr["next"]["text"] == "Связаться" and imp[-1]["email"] == "solo@example.com" and any(v["name"] == "Импортова, Анна" and v["telegram"] == "@import_anna" for v in imp), (len(studs()) - n1, petr))
      # финансы
      pg.click(".nav >> text=Аналитика"); pg.click(".tabs >> text=Финансы"); pg.wait_for_selector(".chart"); pg.wait_for_timeout(200)
      check("finance chart and tables", pg.locator(".chart path").count() >= 3 and pg.locator("text=Куда идёт чистая выручка").count() >= 1 and pg.locator(".kpi").count() == 4)
      pg.locator(".chart g").nth(5).hover(); pg.wait_for_timeout(150)
      check("chart tooltip", pg.locator("#tip").is_visible() and "Получено" in pg.inner_text("#tip"), pg.inner_text("#tip"))
      fin = pg.evaluate("(() => { const f = finance(D().students); return { total: f.total, sumMonths: f.months.reduce((a, m) => a + m.got, 0), src: f.sources.reduce((a, r) => a + r.revenue, 0), coh: f.cohorts.reduce((a, r) => a + r.revenue, 0), exp: f.expected, overdue: f.overdue }; })()")
      allpaid = sum(p["amount"] for v in studs().values() for p in v["payments"] if p["paid"])
      check("finance totals consistent", fin["total"] == allpaid == fin["src"] == fin["coh"] and fin["sumMonths"] <= fin["total"], fin)
      pg.mouse.move(5, 5); shot(pg, "s17_finance.png", full_page=True)
      pg.click(".tabs >> text=Воронка и этапы"); pg.wait_for_timeout(150)
      # список, аналитика, платежи, партнёры, настройки
      pg.click(".nav >> text=Студенты"); pg.wait_for_timeout(200); shot(pg, "s5_list.png")
      pg.fill("#f-q", "Тестов"); pg.wait_for_timeout(400); check("list search keeps focus", pg.locator("tbody tr.click").count() == 1 and pg.evaluate("document.activeElement.id") == "f-q")
      pg.click("[data-act=freset]"); pg.click("[data-act=csv]"); pg.wait_for_timeout(200); check("csv", "Следующий шаг" in pg.evaluate("window.__saved.data"))
      pg.click(".nav >> text=Аналитика"); pg.wait_for_timeout(200); shot(pg, "s6_analytics.png", full_page=True)
      check("analytics", pg.locator(".kpi").count() == 6 and pg.locator("text=Почему уходят").count() == 1)
      pg.click(".nav >> text=Платежи"); pg.wait_for_timeout(200); shot(pg, "s7_payments.png")
      pg.click(".nav >> text=Партнёры"); pg.wait_for_timeout(200); pg.locator("tr.click").first.click(); pg.wait_for_timeout(200)
      pg.click("text=Выдать партнёру доступ"); pg.wait_for_selector("#mform")
      check("partner access form is prefilled", pg.input_value("#m-role") == "partner" and pg.input_value("#m-partnerId") != "" and pg.input_value("#m-name") != "")
      pg.fill("#m-username", "blogger"); pg.fill("#m-password", PASSWORD); pg.click("#m-submit"); pg.wait_for_function("() => D().reports.length === 1 && S.accounts.some(a => a.username === 'blogger')", timeout=10000)
      pg.wait_for_timeout(300); shot(pg, "s8_partner.png", full_page=True)
      check("partner gets report with access, no publish step", any(k.startswith("reports/") for k in store()) and pg.locator("text=Выдать партнёру доступ").count() == 0 and "blogger" in pg.inner_text(".page-head"))
      pg.click(".nav >> text=Настройки"); pg.wait_for_timeout(200); shot(pg, "s9_settings.png", full_page=True)
      pg.click("[data-act=backup]"); pg.wait_for_timeout(300)
      bk = json.loads(pg.evaluate("window.__saved.data")); open(Path(tmp) / "backup.json", "w", encoding="utf-8").write(json.dumps(bk, ensure_ascii=False))
      check("backup v6 with next, modules, progress, meetings, directions", bk["version"] == 6 and len(bk["data"]["directions"]) == 1 and len(bk["data"]["expenses"]) >= 2 and len(bk["data"]["meetings"]) >= 8 and any(c.get("modules") for c in bk["data"]["cohorts"]) and any(s.get("progress") for s in bk["data"]["students"]) and any(s.get("next") for s in bk["data"]["students"]))
      pg.click(".nav >> text=Сегодня"); pg.wait_for_timeout(300)
      # --- календарь встреч ---
      meets = lambda: {k: v for k, v in store().items() if k.startswith("meetings/")}
      pg.click(".nav >> text=Календарь"); pg.wait_for_selector(".cal-grid"); pg.wait_for_timeout(200)
      shot(pg, "s18_calendar.png", full_page=True)
      check("calendar grid", pg.locator(".cal-cell").count() in (28, 35, 42) and pg.locator(".cal-cell.now.sel").count() == 1 and pg.locator(".cal-cell .ev").count() >= 3, pg.locator(".cal-cell .ev").count())
      check("unmarked past meetings panel", pg.locator("text=Прошли и не отмечены").count() == 1)
      check("interviews are derived events", pg.evaluate("events('0000-00-00','9999-99-99').filter(e => e.type === 'i').length === D().students.filter(isActive).flatMap(s => (s.apps||[]).filter(a => a.interview && !['rejected','accepted'].includes(a.status))).length"))
      n_m = len(meets())
      pg.click(".page-head >> text=+ Встреча"); pg.wait_for_selector("#mform"); shot(pg, "s19_meeting_form.png")
      pg.fill("#m-title", "Тестовая встреча"); pg.fill("#m-link", "javascript:alert(1)"); pg.click("#m-submit"); pg.wait_for_timeout(150)
      check("meeting link scheme checked", "http" in pg.inner_text("#m-err") and len(meets()) == n_m)
      pg.fill("#m-link", "https://example.com/room"); pg.fill("#m-time", "09:15"); pg.select_option("#m-studentId", index=1); msid = pg.input_value("#m-studentId")
      pg.click("#m-submit"); pg.wait_for_timeout(300)
      mk = [k for k, v in meets().items() if v["title"] == "Тестовая встреча"]
      mt = meets()[mk[0]] if mk else {}
      check("meeting created", len(mk) == 1 and mt["date"] == today and mt["time"] == "09:15" and mt["studentId"] == msid and mt["status"] == "planned" and mt["link"].startswith("https://"), mt)
      row = pg.locator(".cal-wrap .rows li", has_text="Тестовая встреча").first
      check("meeting link rendered safely", row.locator("a[href='https://example.com/room'][rel=noopener]").count() == 1)
      notes0 = len(store()["students/" + msid].get("notes", []))
      row.locator("text=Состоялась").click(); pg.wait_for_timeout(300)
      st = store()["students/" + msid]
      check("meeting done -> note in student", meets()[mk[0]]["status"] == "done" and len(st["notes"]) == notes0 + 1 and st["notes"][-1].get("kind") == "meet" and "Тестовая встреча" in st["notes"][-1]["text"])
      check("meeting note id is tied to meeting", st["notes"][-1]["id"] == "meet-" + mk[0].split("/")[1])
      pg.click("#toast >> text=Отменить"); pg.wait_for_timeout(300)
      pg.evaluate(f"(async () => {{ const id = {json.dumps(mk[0].split('/')[1])}; await Promise.all([meetMark(id, 'done'), meetMark(id, 'done')]); }})()"); pg.wait_for_timeout(400)
      check("double mark gives one note", sum(1 for n in store()["students/" + msid]["notes"] if n.get("kind") == "meet") == 1)
      # форма встречи открыта, а отметку ставит другой человек: правка названия не затирает статус
      pg.locator(".cal-wrap .rows li", has_text="Тестовая встреча").first.locator(".who").click(); pg.wait_for_selector("#mform")
      pg.evaluate(f"S.db.collection('meetings').doc({json.dumps(mk[0].split('/')[1])}).update({{notes: 'чужая заметка'}})"); pg.wait_for_timeout(150)
      pg.fill("#m-duration", "30"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      check("meeting form keeps others' edits", meets()[mk[0]]["notes"] == "чужая заметка" and meets()[mk[0]]["duration"] == 30 and meets()[mk[0]]["status"] == "done", meets()[mk[0]])
      pg.locator(".cal-wrap .rows li", has_text="Тестовая встреча").first.locator(".who").click(); pg.wait_for_selector("#mform")
      pg.select_option("#m-status", "planned"); pg.click("#m-submit"); pg.wait_for_timeout(400)
      check("undo meeting mark", meets()[mk[0]]["status"] == "planned" and len(store()["students/" + msid]["notes"]) == notes0)
      pg.locator(".cal-wrap .rows li", has_text="Тестовая встреча").first.locator(".who").click(); pg.wait_for_selector("#mform")
      pg.fill("#m-date", tomorrow); pg.click("#m-submit"); pg.wait_for_timeout(300)
      check("meeting rescheduled, calendar follows", meets()[mk[0]]["date"] == tomorrow and pg.locator(f"#cal-{tomorrow}.sel").count() == 1 and meets()[mk[0]]["title"] == "Тестовая встреча")
      h = pg.inner_text(".cal-bar h2"); pg.click("[data-act=cal-go][data-n='1']"); pg.wait_for_timeout(150); h2 = pg.inner_text(".cal-bar h2")
      pg.click(".cal-bar >> text=Сегодня"); pg.wait_for_timeout(150)
      check("month navigation", h2 != h and pg.locator(".cal-cell.now.sel").count() == 1, (h, h2))
      pg.click(".nav >> text=Сегодня"); pg.wait_for_timeout(200)
      check("today shows meetings panel", pg.locator(".panel", has_text="Встречи на неделе").locator("li").count() >= 3)
      # карточка: раздел встреч и новая встреча из карточки
      pg.click(".nav >> text=Студенты"); pg.wait_for_timeout(150); pg.fill("#f-q", ""); pg.wait_for_timeout(250)
      pg.evaluate(f"openStudent({json.dumps(msid)})"); pg.wait_for_timeout(250)
      check("drawer meetings section", pg.locator(".drawer .sec", has_text="Встречи").locator("tr").count() >= 1)
      pg.locator(".drawer [data-act=meet-new]").click(); pg.wait_for_selector("#mform")
      check("meeting from drawer prefilled", pg.input_value("#m-studentId") == msid and pg.input_value("#m-title") == "Созвон"); pg.keyboard.press("Escape")
      # --- журнал денег ---
      lid = pg.evaluate("D().students.find(s => (s.payments||[]).some(p => !p.paid) && !(s.log||[]).length && s.partnerId && D().reports.some(r => r.id === s.partnerId)).id")
      rid = store()["students/" + lid]["partnerId"]; rev0 = store()["reports/" + rid]["revenue"]
      pg.evaluate(f"openStudent({json.dumps(lid)})"); pg.wait_for_timeout(250)
      pg.locator(".drawer [data-act=pay-done]").first.click(); pg.wait_for_timeout(300)
      lg = store()["students/" + lid].get("log", [])
      check("pay-done logged", len(lg) == 1 and "отмечен оплаченным" in lg[0]["text"], lg)
      pg.click("#toast >> text=Отменить"); pg.wait_for_timeout(300)
      check("undo removes own log entry", len(store()["students/" + lid].get("log", [])) == 0)
      pg.locator(".drawer [data-act=pay-done]").first.click(); pg.wait_for_timeout(300)
      pg.locator(".drawer [data-act=pay-edit]").first.click(); pg.wait_for_selector("#mform"); pg.fill("#m-amount", "12345"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      lg = store()["students/" + lid]["log"]
      check("payment edit logged with old and new sum", len(lg) == 2 and "сумма" in lg[-1]["text"] and "12" in lg[-1]["text"], lg)
      check("money log shown in drawer", pg.locator("#d-log summary").count() == 1 and "2" in pg.inner_text("#d-log summary"))
      try: pg.wait_for_function(f"() => D().reports.find(r => r.id === {json.dumps(rid)}).revenue !== {rev0}", timeout=15000)
      except Exception: pass
      check("partner report refreshed by itself", store()["reports/" + rid]["revenue"] != rev0, (rev0, store()["reports/" + rid]["revenue"]))
      # --- форма студента пишет только изменённые поля ---
      pg.locator(".drawer [data-act=student-edit]").click(); pg.wait_for_selector("#mform")
      pg.evaluate(f"S.db.collection('students').doc({json.dumps(lid)}).update({{comment: 'чужая правка', price: 77777}})"); pg.wait_for_timeout(200)
      pg.fill("#m-city", "Тверь"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      st = store()["students/" + lid]
      check("student form keeps others' edits", st["city"] == "Тверь" and st["comment"] == "чужая правка" and st["price"] == 77777, (st["city"], st["comment"], st["price"]))
      pg.locator(".drawer [data-act=student-edit]").click(); pg.wait_for_selector("#mform"); pg.fill("#m-price", "88000"); pg.click("#m-submit"); pg.wait_for_timeout(300)
      check("price change logged", "Стоимость по договору" in store()["students/" + lid]["log"][-1]["text"])
      # --- корзина ---
      n_s = pg.evaluate("D().students.length")
      pg.locator(".drawer [data-act=student-edit]").click(); pg.wait_for_selector("#mform"); pg.click("[data-act=m-delete]"); pg.click("[data-act=m-delete]"); pg.wait_for_timeout(300)
      check("delete goes to trash", bool(store()["students/" + lid].get("deletedAt")) and pg.evaluate("D().students.length") == n_s - 1 and pg.evaluate("S.trash.length") == 1 and pg.locator(".drawer").count() == 0)
      pg.click("#toast >> text=Отменить"); pg.wait_for_timeout(300)
      check("undo restores from trash", not store()["students/" + lid].get("deletedAt") and pg.evaluate("D().students.length") == n_s)
      pg.evaluate(f"trashStudent({json.dumps(lid)})"); pg.wait_for_timeout(300); pg.evaluate(f"trashStudent({json.dumps(msid)})"); pg.wait_for_timeout(300)
      pg.click(".nav >> text=Настройки"); pg.wait_for_timeout(200); shot(pg, "s20_trash.png", full_page=True)
      check("trashed student's meetings hidden", pg.evaluate(f"events('0000-00-00','9999-99-99').every(e => !e.m || e.m.studentId !== {json.dumps(msid)}) && D().meetings.some(m => m.studentId === {json.dumps(msid)})"))
      check("trash panel", pg.locator(".panel", has_text="Корзина").locator("li").count() == 2)
      pg.click("[data-act=backup]"); pg.wait_for_timeout(300); bk2 = json.loads(pg.evaluate("window.__saved.data"))
      check("backup keeps trashed students", sum(1 for x in bk2["data"]["students"] if x.get("deletedAt")) == 2)
      pg.locator(f"[data-act=trash-restore][data-id='{lid}']").click(); pg.wait_for_timeout(300)
      check("restore from settings", not store()["students/" + lid].get("deletedAt") and pg.evaluate("S.trash.length") == 1)
      btn = pg.locator(f"[data-act=trash-purge][data-id='{msid}']"); btn.click(); pg.wait_for_timeout(100); pg.locator(f"[data-act=trash-purge][data-id='{msid}']").click(); pg.wait_for_timeout(400)
      check("purge removes student and personal meetings", ("students/" + msid) not in store() and not any(v.get("studentId") == msid for v in meets().values()) and pg.locator(".panel", has_text="Корзина").count() == 0)
      # --- сортировка таблицы ---
      pg.click(".nav >> text=Студенты"); pg.wait_for_timeout(200)
      names = lambda: pg.locator("tbody tr.click td:nth-child(2) strong").all_inner_texts()
      pg.click("#th-name"); pg.wait_for_timeout(150); a1 = names(); pg.click("#th-name"); pg.wait_for_timeout(150); a2 = names()
      check("sort by name asc/desc", a1 == sorted(a1, key=str.lower) and a2 == a1[::-1] and pg.evaluate("document.activeElement.id") == "th-name", a1[:3])
      pg.click("#th-name"); pg.wait_for_timeout(150); pg.click("#th-paid"); pg.wait_for_timeout(150)
      check("sort by paid", pg.evaluate("S.sort.k === 'paid' && S.visible.map(id => paidSum(stu(id))).every((v, i, a) => !i || a[i - 1] <= v)"))
      pg.click("#th-paid"); pg.click("#th-paid"); pg.wait_for_timeout(150); check("sort off on third click", pg.evaluate("S.sort === null"))
      # партнёр
      # --- учётные записи и роли ---
      pg.click(".nav >> text=Настройки"); pg.wait_for_timeout(300)
      check("access panel lists admin", pg.locator(".panel", has_text="Доступ").locator("li", has_text="admin").count() == 1 and pg.locator(".panel", has_text="Учётная запись").count() == 1)
      mentor_team = pg.evaluate("(() => { const by = {}; for (const s of D().students) if (s.mentorId) by[s.mentorId] = (by[s.mentorId] || 0) + 1; const id = Object.keys(by).sort((a, b) => by[b] - by[a])[0]; return { id, n: by[id], name: teamName(id) }; })()")
      pg.click("[data-act=account-new]"); pg.wait_for_selector("#mform"); shot(pg, "s21_account_form.png")
      team0 = pg.evaluate("D().team.length")
      pg.fill("#m-username", "irina"); pg.fill("#m-name", "Ирина Ментор"); pg.select_option("#m-role", "mentor"); pg.fill("#m-password", "123"); pg.click("#m-submit"); pg.wait_for_timeout(600)
      check("weak password rejected with reason, no orphan team entry", pg.locator("#mform").count() == 1 and len(pg.inner_text("#m-err")) > 10 and pg.evaluate("D().team.length") == team0, (pg.inner_text("#m-err"), pg.evaluate("D().team.length"), team0))
      pg.select_option("#m-teamId", mentor_team["id"])
      pg.fill("#m-password", PASSWORD); pg.click("#m-submit"); pg.wait_for_timeout(600)
      check("mentor account created", pg.locator("#mform").count() == 0 and pg.locator(".panel", has_text="Доступ").locator("li", has_text="irina").count() == 1)
      # новый ментор одним шагом: запись в команде заводится вместе с доступом
      pg.click("[data-act=account-new]"); pg.wait_for_selector("#mform"); pg.fill("#m-username", "oleg"); pg.fill("#m-name", "Олег Новый"); pg.fill("#m-password", PASSWORD); pg.click("#m-submit")
      pg.wait_for_function("() => S.accounts.some(a => a.username === 'oleg')", timeout=10000)
      check("mentor and team entry created in one step", pg.evaluate("D().team.some(m => m.name === 'Олег Новый' && S.accounts.some(a => a.username === 'oleg' && a.linkId === m.id))"))
      pkeys = "['kind', 'name', 'promo', 'leads', 'paidStudents', 'conv', 'employed', 'revenue', 'months', 'accrued', 'paidOut', 'balance', 'payouts', 'rows']"
      parity = pg.evaluate(f"(() => {{ SETTLED = null; const a = partyReport('partner', {json.dumps(rid)}), b = D().reports.find(r => r.id === {json.dumps(rid)}); const pick = d => JSON.stringify([...{pkeys}.map(k => d[k]), d.funnel.map(f => [f.label, f.count, f.pct])]); return pick(a) === pick(b) ? 'same' : pick(a) + ' | ' + pick(b); }})()")
      check("server report equals page calculation", parity == "same", parity[:900])
      shot(pg, "s22_settings_server.png", full_page=True)
      # ментор: только свои студенты, без партнёров и финансов
      mn_ctx = b.new_context(viewport={"width": 1440, "height": 900}); mn_ctx.add_init_script(INIT)
      mn = mn_ctx.new_page(); mn.on("pageerror", lambda e: errs.append("MENTOR " + str(e)))
      login(mn, "irina"); mn.wait_for_selector(".today", timeout=10000); mn.wait_for_timeout(400); shot(mn, "s23_mentor_today.png", full_page=True)
      seen = mn.evaluate("({ n: D().students.length, own: D().students.every(s => s.mentorId === myTeam().id), payouts: D().payouts.length, reports: D().reports.length, limited: S.limited, contacts: D().partners.some(p => 'contacts' in p) })")
      check("mentor sees only own students", seen["n"] == mentor_team["n"] and seen["own"] and seen["limited"], (seen, mentor_team))
      check("mentor has no payouts, only own report, no partner contacts", seen["payouts"] == 0 and seen["reports"] == 1 and not seen["contacts"], seen)
      check("mentor menu has no partners", mn.locator(".nav >> text=Партнёры").count() == 0)
      mn.click(".nav >> text=Выплаты"); mn.wait_for_selector(".kpi")
      mkeys = list(mn.evaluate("window.__store").keys())
      check("mentor sees only own accruals", mn.locator("#pay-parties").count() == 0 and mn.locator("#pay-dirs").count() == 0 and mn.locator(".kpi").count() == 4 and "Начисления по месяцам" in mn.text_content("#main")
            and not any(k.startswith(("payouts/", "directions/", "expenses/")) for k in mkeys) and [k for k in mkeys if k.startswith("reports/")] == ["reports/" + mn.evaluate("myTeam().id")], [k for k in mkeys if not k.startswith(("students/", "meetings/"))])
      mn.click(".nav >> text=Аналитика"); mn.wait_for_timeout(200); check("mentor has no finance tab", mn.locator(".tabs >> text=Финансы").count() == 0 and mn.locator(".kpi").count() == 6)
      mn.click(".nav >> text=Настройки"); mn.wait_for_timeout(200); check("mentor settings: account only", mn.locator(".panel").count() <= 2 and mn.locator("text=Сменить пароль").count() == 1 and mn.locator("text=Резервная копия").count() == 0)
      foreign = pg.evaluate(f"D().students.find(s => s.mentorId !== {json.dumps(mentor_team['id'])}).id")
      denied = mn.evaluate(f"S.db.collection('students').doc({json.dumps(foreign)}).update({{ name: 'Взлом' }}).then(() => 'written', e => e.code)")
      check("mentor cannot write a foreign student", denied in ("permission_denied", "not_found") and store()["students/" + foreign]["name"] != "Взлом", denied)
      mn.click(".nav >> text=Студенты"); mn.wait_for_timeout(200); mn.click("text=+ Заявка"); mn.fill("#m-name", "Заявка Ментора"); mn.fill("#m-contact", "@mentor_lead"); mn.click("#m-submit"); mn.wait_for_timeout(500)
      check("mentor's lead is assigned to the mentor", mn.evaluate("D().students.some(s => s.name === 'Заявка Ментора' && s.mentorId === myTeam().id)"))
      try: pg.wait_for_function("() => D().students.some(s => s.name === 'Заявка Ментора')", timeout=15000); got = True
      except Exception: got = False
      check("admin page receives mentor's change by itself", got)
      # заявка с сайта попадает на экран администратора
      lead = post("api/leads/", {"name": "Сайтова Вера", "phone": "+7 955 000-11-22", "promo": "BLOG1C"})
      dup = post("api/leads/", {"name": "Вера", "phone": "8 955 000 11 22"})
      try: pg.wait_for_function("() => { const s = D().students.find(s => s.name === 'Сайтова Вера'); return s && (s.notes || []).length === 1; }", timeout=15000)
      except Exception: pass
      site = pg.evaluate("D().students.find(s => s.name === 'Сайтова Вера')")
      check("site lead arrives with partner and next step", bool(site) and site["next"]["text"] == "Связаться" and bool(site["partnerId"]) and site["partnerShare"] == 40 and dup == {"ok": True} and lead == {"ok": True} and len(site["notes"]) == 1, site)
      check("mentor does not see unassigned site lead", not mn.evaluate("D().students.some(s => s.name === 'Сайтова Вера')"))
      pg.click(".nav >> text=Сегодня"); pg.wait_for_timeout(300); shot(pg, "s24_no_mentor.png", full_page=True)
      orphan = pg.locator(".panel", has_text="Без ментора").locator("li", has_text="Сайтова Вера")
      check("unassigned site lead is shown to admin", orphan.count() == 1 and "заявка с сайта" in orphan.inner_text())
      orphan.locator("[data-act=assign]").click(); pg.wait_for_selector("#mform"); pg.select_option("#m-mentorId", mentor_team["id"]); pg.click("#m-submit")
      try: mn.wait_for_function("() => D().students.some(s => s.name === 'Сайтова Вера')", timeout=15000); got = True
      except Exception: got = False
      check("assigned lead appears for the mentor", got and pg.locator(".panel", has_text="Без ментора").locator("li", has_text="Сайтова Вера").count() == 0)
      mn.click(".nav >> text=Настройки"); mn.click(".panel >> text=Выйти"); mn.wait_for_selector("input[name=password]")
      mn.goto(URL); check("logout ends the session", "/login/" in mn.url, mn.url)
      # --- Telegram: ссылки партнёров, подключение уведомлений, заявка через бота ---
      pg.click(".nav >> text=Настройки"); pg.wait_for_timeout(300)
      tgp = pg.locator(".panel", has_text="Заявки из Telegram")
      check("telegram panel lists partner links", tgp.locator("li", has_text="https://t.me/menti_e2e_bot?start=BLOG1C").count() == 1 and tgp.locator("li", has_text="Общая ссылка").count() == 1, tgp.inner_text()[:300])
      tgp.locator("li", has_text="BLOG1C").locator("[data-act=copy]").click(); pg.wait_for_timeout(200)
      check("partner bot link copied", pg.evaluate("navigator.clipboard.readText()") == "https://t.me/menti_e2e_bot?start=BLOG1C")
      shot(pg, "s25_telegram_settings.png", full_page=True)
      pg.click("[data-act=tg-link]"); pg.wait_for_selector("#mform a.btn")
      href = pg.get_attribute("#mform a.btn", "href")
      check("staff telegram link offered", href.startswith("https://t.me/menti_e2e_bot?start=link_") and pg.get_attribute("#mform a.btn", "rel") == "noopener", href)
      pg.keyboard.press("Escape")
      n_before = pg.evaluate("D().students.length")
      codes = [tg_say("/start " + href.split("start=")[1], chat=1001), tg_say("/start BLOG1C"),
               tg({"update_id": 2, "callback_query": {"id": "c", "data": "consent", "from": {"id": 4242, "username": "gleb_tg", "first_name": "Глеб"}, "message": {"message_id": 1, "chat": {"id": 4242, "type": "private"}}}}),
               tg_say("Ботов Глеб"), tg_say("Хочу в аналитики 1С")]
      try: pg.wait_for_function("() => D().students.some(s => s.name === 'Ботов Глеб')", timeout=15000)
      except Exception: pass
      bot_lead = pg.evaluate("D().students.find(s => s.name === 'Ботов Глеб')")
      check("lead from telegram bot arrives with partner", codes == [200] * 5 and bool(bot_lead) and bot_lead["telegram"] == "@gleb_tg" and bot_lead["source"] == "telegram" and bot_lead["partnerShare"] == 40 and bot_lead["consent"] is True and bot_lead["comment"] == "Хочу в аналитики 1С" and pg.evaluate("D().students.length") == n_before + 1, (codes, bot_lead))
      pg.reload(); pg.wait_for_function("() => typeof S !== 'undefined' && S.status === 'ready' && S.loaded.students", timeout=15000)
      check("staff telegram linked after pressing start", pg.evaluate("S.me.telegram.linked === true"))
      pg.click(".nav >> text=Сегодня"); pg.wait_for_timeout(300)
      check("telegram lead shown in no-mentor panel", "заявка из Telegram" in pg.locator(".panel", has_text="Без ментора").locator("li", has_text="Ботов Глеб").inner_text())
      # владелец добавляет себя в команду одной кнопкой
      pg.click(".nav >> text=Настройки"); pg.wait_for_timeout(300)
      check("owner is prompted to join the team", pg.locator("text=Вас нет в команде").count() == 1 and pg.evaluate("!myTeam()"))
      pg.click("[data-act=self-team]"); pg.wait_for_function("() => typeof S !== 'undefined' && S.status === 'ready' && S.loaded.team && !!myTeam()", timeout=15000)
      # направления: чистая выручка, доли сторон, расходы, выплаты
      mon = pg.evaluate("today().slice(0, 7)")
      pg.click(".nav >> text=Настройки"); pg.wait_for_timeout(200)
      check("settings list the demo direction with its terms", "Аналитик 1С с нуля" in pg.locator(".panel", has_text="Направления").first.inner_text() and "вам 40%" in pg.locator(".panel", has_text="Направления").first.inner_text())
      dm = pg.evaluate("(() => { const d = D().directions[0]; return { id: d.id, parties: termsFor(d, today().slice(0, 7)) }; })()")
      cell = lambda m=None: pg.evaluate(f"(() => {{ SETTLED = null; const c = settle().find(c => c.directionId === {json.dumps(dm['id'])} && c.month === {json.dumps(m or mon)}); return c ? {{ received: c.received, expenses: c.expenses, net: c.net, owner: c.owner, parties: c.parties }} : null; }})()")
      c0 = cell()
      check("net revenue = received - expenses, everyone gets a share of it", c0 and abs(c0["net"] - (c0["received"] - c0["expenses"])) < 0.01 and c0["expenses"] > 0
            and all(abs(x["accrued"] - round(c0["net"] * x["share"]) / 100) < 0.01 for x in c0["parties"]) and abs(c0["owner"] + sum(x["accrued"] for x in c0["parties"]) - c0["net"]) < 0.01, c0)
      pg.click(".nav >> text=Выплаты"); pg.wait_for_selector("#pay-month")
      check("payouts page: direction panel, parties and expenses", pg.locator("#pay-dirs .panel").count() >= 1 and pg.locator("#pay-parties tbody tr").count() == 3 and pg.locator("#pay-expenses li").count() >= 1 and pg.locator(".kpi").count() == 5)
      pg.click(".page-head [data-act=expense-new]"); pg.wait_for_selector("#mform"); pg.fill("#m-amount", "10000"); pg.fill("#m-comment", "Непредвиденный расход"); pg.click("#m-submit"); pg.wait_for_selector("#mform", state="detached"); pg.wait_for_timeout(400)
      c1 = cell()
      check("an unforeseen expense lowers net revenue and every share", abs(c0["net"] - c1["net"] - 10000) < 0.01 and all(abs(a["accrued"] - b["accrued"] - 100 * a["share"]) < 0.01 for a, b in zip(c0["parties"], c1["parties"])) and abs(c0["owner"] - c1["owner"] - 4000) < 0.01, (c0, c1))
      tm = pg.evaluate(f"(() => {{ const x = {json.dumps(dm['parties'])}.find(x => x.kind === 'mentor'); return {{ id: x.id, name: teamById(x.id).name, share: x.share }}; }})()")
      bal0 = pg.evaluate(f"partyStats('mentor', {json.dumps(tm['id'])}).balance")
      row = pg.locator("#pay-parties tbody tr", has_text=tm["name"])
      row.locator("[data-act=payout-new]").click(); pg.wait_for_selector("#mform"); pg.fill("#m-amount", "1000"); pg.click("#m-submit"); pg.wait_for_selector("#mform", state="detached"); pg.wait_for_timeout(400)
      mp = [v for k, v in pg.evaluate("window.__store").items() if k.startswith("payouts/") and v.get("mentorId") == tm["id"]]
      check("mentor payout recorded and balance reduced", len(mp) == 1 and mp[0]["amount"] == 1000 and "partnerId" not in mp[0] and abs(bal0 - pg.evaluate(f"(SETTLED = null, partyStats('mentor', {json.dumps(tm['id'])}).balance)") - 1000) < 0.01, mp)
      row.click(); pg.wait_for_timeout(200); check("party details: months and payouts", pg.locator(".panel", has_text=tm["name"] + ": начисления").count() == 1 and pg.locator(".panel", has_text=tm["name"] + ": выплаты").locator("[data-act=payout-del]").count() == 1)
      shot(pg, "s27_payouts.png", full_page=True)
      # новые условия действуют с выбранного месяца и не трогают прошлое
      old = pg.evaluate(f"(() => {{ SETTLED = null; const c = settle().find(c => c.directionId === {json.dumps(dm['id'])} && c.month < {json.dumps(mon)} && c.received > 0); return c ? {{ month: c.month, parties: c.parties }} : null; }})()")
      pg.click(".nav >> text=Настройки"); pg.wait_for_timeout(200); pg.locator(".panel", has_text="Направления").first.locator("[data-act=direction-edit]").first.click(); pg.wait_for_selector("#mform")
      bl = next(x for x in dm["parties"] if x["kind"] == "partner")
      pg.fill("#m-p_" + bl["id"], "95"); check("form warns when shares exceed 100%", "больше 100%" in pg.inner_text("#m-own"), pg.inner_text("#m-own"))
      pg.click("#m-submit"); pg.wait_for_timeout(200); check("shares above 100% are rejected", pg.locator("#mform").count() == 1 and "больше 100%" in pg.inner_text("#m-err"))
      pg.fill("#m-p_" + bl["id"], "50"); check("form shows what is left to the owner", "Вам остаётся: 20%" in pg.inner_text("#m-own"), pg.inner_text("#m-own"))
      pg.click("#m-submit"); pg.wait_for_selector("#mform", state="detached"); pg.wait_for_timeout(400)
      c2, o2 = cell(), cell(old["month"]) if old else None
      share = lambda c: next(x["share"] for x in c["parties"] if x["kind"] == "partner")
      check("new terms apply from this month, past months keep the old ones", share(c2) == 50 and old is not None and share(o2) == 30 and o2["parties"] == old["parties"], (c2["parties"], old))
      # сервер считает те же отчёты, что и страница
      same = "(() => { const pick = (d, k) => JSON.stringify(k.map(x => x === 'funnel' ? d.funnel.map(f => [f.label, f.count, f.pct]) : d[x])); SETTLED = null;" \
             f" const a = partyReport('partner', {json.dumps(rid)}), b = D().reports.find(r => r.id === {json.dumps(rid)});" \
             " const pk = ['kind', 'name', 'promo', 'leads', 'paidStudents', 'conv', 'employed', 'revenue', 'funnel', 'months', 'accrued', 'paidOut', 'balance', 'payouts', 'rows'];" \
             " const mid = S.accounts.find(x => x.role === 'mentor' && x.linkId).linkId, c = partyReport('mentor', mid), e = D().reports.find(r => r.id === mid);" \
             " const mk = ['kind', 'name', 'months', 'accrued', 'paidOut', 'balance', 'payouts'];" \
             " return !!b && !!e && pick(a, pk) === pick(b, pk) && pick(c, mk) === pick(e, mk) && a.months.length + c.months.length > 1; })()"
      try: pg.wait_for_function("() => " + same, timeout=15000); par = True
      except Exception: par = False
      check("server reports for the blogger and the mentor equal the page calculation", par, pg.evaluate(f"JSON.stringify([partyReport('partner', {json.dumps(rid)}).months.slice(0, 2), (D().reports.find(r => r.id === {json.dumps(rid)}) || {{}}).months])")[:900] if not par else "")
      pg.click(".nav >> text=Аналитика"); pg.click(".tabs >> text=Финансы"); pg.wait_for_selector("#chart-split"); check("finance: net revenue split chart", pg.locator("#chart-split .s1").count() >= 1 and pg.locator("#chart-split .s3").count() >= 1)
      shot(pg, "s28_finance.png", full_page=True)
      pg.click(".tabs >> text=Воронка и этапы"); pg.wait_for_selector("#chart-leads"); check("analytics: leads by month and mentors table", pg.locator("#chart-leads rect, #chart-leads path").count() > 0 and pg.locator("#an-mentors tbody tr").count() >= 1)
      shot(pg, "s29_analytics.png", full_page=True)
      # тема и раскладка настроек
      pg.click(".nav >> text=Настройки"); pg.wait_for_selector(".cols2")
      tops = pg.evaluate("[...document.querySelectorAll('.cols2 > .stack')].map(e => Math.round(e.getBoundingClientRect().top))")
      check("settings: two columns start at the same height", len(tops) == 2 and tops[0] == tops[1], tops)
      pg.locator(".seg >> text=Тёмная").click(); pg.wait_for_timeout(150)
      check("theme: dark is applied and marked", pg.evaluate("document.documentElement.dataset.theme") == "dark" and pg.locator(".seg button.on", has_text="Тёмная").count() == 1)
      pg.reload(); pg.wait_for_selector(".cols2"); check("theme survives reload", pg.evaluate("document.documentElement.dataset.theme") == "dark")
      pg.click("#sync .tbtn"); pg.wait_for_timeout(150); check("theme button in the menu switches to light", pg.evaluate("document.documentElement.dataset.theme") == "light")
      pg.locator(".seg >> text=Как в системе").click(); pg.wait_for_timeout(150); check("theme: back to system", pg.evaluate("document.documentElement.dataset.theme") is None)
      shot(pg, "s26_settings.png", full_page=True)
      # сквозной путь: от заявки до оффера одной кнопкой «Дальше»
      pg.click(".nav >> text=Сегодня"); pg.click("[data-act=quick]"); pg.wait_for_selector("#mform"); pg.fill("#m-name", "Путь Сквозной"); pg.fill("#m-contact", "@put_skvoznoy"); pg.click("#m-submit"); pg.wait_for_timeout(400)
      fid = pg.evaluate("D().students.find(s => s.name === 'Путь Сквозной').id")
      doc = lambda: pg.evaluate("window.__store")["students/" + fid]
      pg.evaluate(f"updDoc('students', {json.dumps(fid)}, {{ mentorId: null }})"); pg.wait_for_timeout(500)
      row = pg.locator(f".rows li[data-id='{fid}']").first
      check("lead row: write link and take button", row.locator("a[href='https://t.me/put_skvoznoy']").count() == 1 and row.locator("[data-act=take]").count() == 1)
      row.locator("[data-act=take]").click(); pg.wait_for_timeout(500)
      check("take assigns the lead to me", doc()["mentorId"] == pg.evaluate("myTeam().id"), doc().get("mentorId"))
      pg.evaluate(f"openStudent({json.dumps(fid)})"); pg.wait_for_selector("#d-next")
      check("path strip shows stage 1 of 9", pg.locator(".path li").count() == 9 and pg.locator(".path li.cur").count() == 1 and pg.locator(".path li.done").count() == 0)
      def nxt(label, fill=None):
          check(f"next button offers {label}", label in pg.inner_text("#d-next"), pg.inner_text("#d-next"))
          pg.click("#d-next"); pg.wait_for_selector("#mform")
          for sel, val in (fill or {}).items(): pg.fill(sel, val)
          pg.click("#m-submit"); pg.wait_for_selector("#mform", state="detached"); pg.wait_for_timeout(350)
      nxt("Связались", {"#m-note": "Хочет в аналитики, начнёт в ноябре"})
      d = doc(); check("contacted: note saved and step set", d["stage"] == "contacted" and any(n["text"].startswith("Хочет в аналитики") for n in d["notes"]) and d["next"]["text"] == "Договориться о созвоне", d.get("next"))
      nxt("Созвон", {"#m-mdate": "2026-12-01", "#m-mtime": "15:30"})
      d = doc(); meets = [m for k, m in pg.evaluate("window.__store").items() if k.startswith("meetings/") and m.get("studentId") == fid]
      check("call: meeting created and step on that day", d["stage"] == "call" and len(meets) == 1 and meets[0]["date"] == "2026-12-01" and meets[0]["time"] == "15:30" and d["next"] == {"text": "Созвон", "date": "2026-12-01"}, (meets, d.get("next")))
      nxt("Ждём оплату", {"#m-price": "90000", "#m-parts": "3", "#m-first": "2026-12-05"})
      d = doc(); check("waiting payment: price, three payments, log", d["stage"] == "waiting_payment" and d["price"] == 90000 and [x["amount"] for x in d["payments"]] == [30000, 30000, 30000] and d["payments"][0]["due"] == "2026-12-05" and len(d["log"]) == 2 and d["next"]["text"] == "Проверить оплату", d.get("log"))
      nxt("Обучается")
      d = doc(); check("studying: first payment marked paid with a log entry", d["stage"] == "studying" and d["payments"][0]["paid"] and not d["payments"][1]["paid"] and len(d["log"]) == 3)
      nxt("Итоговый проект"); nxt("Поиск работы")
      nxt("Оффер", {"#m-jobCompany": "ООО Пример", "#m-jobPosition": "Аналитик 1С", "#m-jobSalary": "120000"})
      d = doc(); check("offer: company and salary saved", d["stage"] == "offer" and d["jobCompany"] == "ООО Пример" and d["jobSalary"] == 120000 and d["offerDate"], d.get("jobCompany"))
      check("history has every stage once", [h["to"] for h in d["history"]] == ["new", "contacted", "call", "waiting_payment", "studying", "final_project", "job_search", "offer"], [h["to"] for h in d["history"]])
      check("path strip: seven done, one current", pg.locator(".path li.done").count() == 7 and pg.locator(".path li.cur").count() == 1)
      shot(pg, "s23_path.png")
      pg.click("#toast button"); pg.wait_for_timeout(500)
      d = doc(); check("undo returns stage and offer fields", d["stage"] == "job_search" and not d.get("jobCompany") and [h["to"] for h in d["history"]][-1] == "job_search", (d["stage"], d.get("jobCompany")))
      pg.keyboard.press("Escape"); pg.wait_for_timeout(200)
      check("owner joined the team", pg.evaluate("myTeam().name === 'Анна Владелец' && S.me.linkId === myTeam().id"))
      # партнёр
      pctx = b.new_context(viewport={"width": 1440, "height": 900}); pctx.add_init_script(INIT)
      pp = pctx.new_page(); pp.on("pageerror", lambda e: errs.append("PARTNER " + str(e)))
      login(pp, "blogger"); pp.wait_for_selector("text=Начисления по месяцам", timeout=10000); pp.wait_for_timeout(200)
      check("partner can change password", pp.locator(".nav >> text=Сменить пароль").count() == 1)
      check("partner sees accruals by month", "Начисления по месяцам" in pp.text_content("#main") and pp.locator(".kpi").count() >= 6)
      check("partner store has only own report", list(pp.evaluate("window.__store").keys()) == ["reports/" + rid], list(pp.evaluate("window.__store").keys())[:5])
      check("partner: no search, no drawer, no contacts, no calendar", pp.locator("#gsearch").is_hidden() and pp.locator("text=Календарь").count() == 0 and "Тестовая встреча" not in pp.inner_text("body") and "+7 9" not in pp.inner_text("body") and "@student" not in pp.inner_text("body"))
      shot(pp, "s10_blogger.png", full_page=True)
      # телефон, тёмная тема
      mctx = b.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", has_touch=True, is_mobile=True)
      m = mctx.new_page(); m.on("pageerror", lambda e: errs.append("MOBILE " + str(e)))
      mctx.add_init_script(INIT); login(m, "admin")
      m.wait_for_selector(".today"); m.wait_for_timeout(300); shot(m, "m1_today.png", full_page=True)
      w = lambda: m.evaluate("document.documentElement.scrollWidth")
      check("mobile today no overflow", w() <= 390, w())
      m.click(".nav >> text=Воронка"); m.wait_for_selector("#board"); m.wait_for_timeout(200); shot(m, "m2_board.png")
      check("mobile board no page overflow", w() <= 390, w())
      sel = m.locator(".col[data-stage=new] .tile-move").first
      check("mobile stage select visible", sel.is_visible())
      mid = m.locator(".col[data-stage=new] .tile").first.get_attribute("data-id")
      sel.select_option("contacted"); m.wait_for_timeout(300)
      check("mobile move via select (no drawer)", m.evaluate("window.__store")["students/" + mid]["stage"] == "contacted" and m.locator(".drawer").count() == 0)
      m.locator(".tile strong").first.tap(); m.wait_for_selector(".drawer"); m.wait_for_timeout(250); shot(m, "m3_drawer.png")
      check("mobile drawer no overflow", w() <= 390, w())
      check("mobile bottom bar: four sections and More", m.locator(".nav > button:visible").count() == 5 and not m.locator(".nav >> text=Потоки").is_visible())
      bar = m.locator(".nav").bounding_box(); check("mobile bar is pinned to the bottom", abs(bar["y"] + bar["height"] - 844) < 2, bar)
      m.click("#d-x") if m.locator(".drawer").count() else None
      m.click(".nav .more"); check("mobile More opens the rest", m.locator(".nav >> text=Потоки").is_visible() and m.get_attribute(".nav .more", "aria-expanded") == "true")
      m.click(".nav .more"); check("mobile More closes", not m.locator(".nav >> text=Потоки").is_visible())
      m.click(".nav >> text=Студенты"); m.wait_for_timeout(200); check("mobile list no overflow", w() <= 390, w())
      m.click(".nav .more"); m.click(".nav >> text=Потоки"); m.wait_for_timeout(200); m.locator(".ccard").first.tap(); m.wait_for_timeout(300); shot(m, "m5_cohort.png"); check("mobile cohort no overflow", w() <= 390, w())
      m.click(".nav .more"); m.click(".nav >> text=Аналитика"); m.click(".tabs >> text=Финансы"); m.wait_for_timeout(300); shot(m, "m6_finance.png", full_page=True); check("mobile finance no overflow", w() <= 390, w()); m.click(".tabs >> text=Воронка и этапы")
      m.click(".nav .more"); m.click(".nav >> text=Аналитика"); m.wait_for_timeout(200); shot(m, "m4_analytics.png", full_page=True); check("mobile analytics no overflow", w() <= 390, w())
      m.click(".nav >> text=Календарь"); m.wait_for_selector(".cal-grid"); m.wait_for_timeout(200); shot(m, "m7_calendar.png", full_page=True); check("mobile calendar no overflow", w() <= 390, w())
      m.locator(".cal-cell.now").tap(); m.wait_for_timeout(200); check("mobile day tap selects", m.locator(".cal-cell.now.sel").count() == 1)
      # тёмная на компьютере
      dk = b.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark").new_page()
      login(dk, "admin"); dk.wait_for_selector(".today"); dk.wait_for_timeout(300)
      shot(dk, "d1_today.png"); dk.click(".nav >> text=Воронка"); dk.wait_for_selector("#board"); dk.locator(".tile").first.click(); dk.wait_for_timeout(300); shot(dk, "d2_drawer.png"); dk.keyboard.press("Escape"); dk.click(".nav >> text=Календарь"); dk.wait_for_selector(".cal-grid"); dk.wait_for_timeout(200); shot(dk, "d3_calendar.png")
      b.close()
finally:
  server.terminate(); server.wait(timeout=10); log.close()
print("ERRORS:", errs); print(f"{len(ok)} passed, {len(bad)} failed:", bad)
sys.exit(1 if bad or errs else 0)
