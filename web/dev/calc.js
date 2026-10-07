// Сверка расчёта денег: считает отчёты сторон тем же кодом, что работает на странице (web/index.html),
// чтобы тест сервера сравнил их с backend/reports.py на одних и тех же данных.
//
// Запуск:  node web/dev/calc.js < cases.json
// Вход:    [{ "data": { "<коллекция>": { "<id>": {…запись…} } }, "parties": [["partner", "p1"], ["mentor", "t1"]] }, …]
// Выход:   [[отчёт, отчёт, …], …] — по списку отчётов на каждый набор данных, в том же порядке.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const page = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const script = page.split("<script>")[1].split("</script>")[0];

// Странице нужен браузер только для отрисовки. Здесь он заменён пустышками: расчёт их не касается.
const el = () => ({
  hidden: false, innerHTML: "", value: "", textContent: "", dataset: {}, style: {},
  classList: { add() { }, remove() { }, toggle() { }, contains: () => false },
  focus() { }, addEventListener() { }, querySelector: () => null, querySelectorAll: () => [],
});
const root = el();
const sandbox = {
  console: { log() { }, warn() { }, error() { } },
  document: { documentElement: root, activeElement: null, addEventListener() { }, querySelector: () => el(), querySelectorAll: () => [], getElementById: () => null },
  location: { hash: "", pathname: "/" }, history: { pushState() { }, replaceState() { } },
  localStorage: { getItem: () => null, setItem() { } }, navigator: {},
  matchMedia: () => ({ matches: false, addEventListener() { } }),
  requestAnimationFrame() { }, setInterval() { }, setTimeout() { }, clearTimeout() { },
  addEventListener() { }, scrollTo() { }, Intl, Date, Math, JSON, URL,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
// Функции страницы объявлены в области её скрипта; дописанная строка выносит наружу только расчёт
vm.runInContext(script + `
;globalThis.__calc = (data, parties) => {
  const rows = name => Object.entries(data[name] || {}).filter(([, d]) => d && typeof d === "object" && !Array.isArray(d)).map(([id, d]) => ({ ...d, id }));
  S.data = { students: rows("students").filter(s => !s.deletedAt), cohorts: rows("cohorts"), partners: rows("partners"), payouts: rows("payouts"),
    team: rows("team"), reports: [], meetings: [], directions: rows("directions"), expenses: rows("expenses") };
  SETTLED = null;
  return parties.map(([kind, id]) => { const r = partyReport(kind, id); delete r.updatedAt; return r; });
};`, sandbox, { filename: "web/index.html" });

const cases = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(c => sandbox.__calc(c.data, c.parties))));
