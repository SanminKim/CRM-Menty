// CRM на своём сервере: тот же интерфейс window.claude, что даёт страница в Claude, поверх API сервера.
// Данные лежат в памяти страницы и обновляются опросом сервера по номеру ревизии.
(function () {
  const POLL_MS = 4000;
  const csrf = () => (document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || "";
  async function call(method, url, data) {
    let res;
    const headers = { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" };
    if (data !== undefined) headers["Content-Type"] = "application/json";
    // Запрос не висит бесконечно: после сна ноутбука или смены сети он обрывается и повторяется
    try { res = await fetch(url, { method, credentials: "same-origin", headers, body: data !== undefined ? JSON.stringify(data) : undefined, signal: AbortSignal.timeout(20000) }); }
    catch (e) { throw { code: "unavailable" }; }
    if (res.status === 401) { location.href = "/login/?next=" + encodeURIComponent(location.pathname); throw { code: "unauthenticated" }; }
    let out = null; try { out = await res.json(); } catch (e) { }
    if (!res.ok) throw { code: (out && out.error) || "internal", message: (out && out.message) || "" };
    return out;
  }

  const cache = {};            // коллекция → Map(id → { rev, data })
  const gone = new Map();      // "коллекция/id" → ревизия удаления: опоздавший ответ не вернёт удалённую запись
  const subs = [];             // { col, fn }
  let rev = null, epoch = null, me = null, ready = null, inflight = null, fails = 0;
  const col = c => cache[c] || (cache[c] = new Map());
  const clone = v => JSON.parse(JSON.stringify(v));
  const emit = cols => { for (const s of subs) if (!cols || cols.has(s.col)) { try { s.fn(); } catch (e) { console.warn(e); } } };

  // Ответ сервера накладывается так, чтобы более старый ответ не затёр более свежую запись
  function apply(res) {
    const touched = new Set();
    // База на сервере стала «старше» нашей (восстановили из копии) или сменились права: берём её целиком
    if (res.full && (res.epoch !== epoch || (rev !== null && res.rev < rev))) {
      for (const c of Object.keys(cache)) { if (cache[c].size) touched.add(c); cache[c].clear(); }
      gone.clear(); rev = null;
    }
    if (res.full) {
      for (const c of Object.keys(cache)) {
        const fresh = new Map((res.docs[c] || []).map(d => [d.id, d]));
        for (const [id, cur] of cache[c]) if (!fresh.has(id) && cur.rev <= res.rev) { cache[c].delete(id); touched.add(c); }
      }
    }
    for (const [c, docs] of Object.entries(res.docs || {})) for (const d of docs) {
      const cur = col(c).get(d.id);
      if ((gone.get(c + "/" + d.id) || 0) >= d.rev) continue;
      if (!cur || cur.rev < d.rev) { col(c).set(d.id, { rev: d.rev, data: d.data }); touched.add(c); } // своя же запись, уже применённая, не перерисовывается
    }
    for (const [c, ids] of Object.entries(res.removed || {})) for (const id of ids) {
      const cur = col(c).get(id);
      if (cur && cur.rev <= res.rev) { col(c).delete(id); touched.add(c); }
    }
    if (rev === null || res.rev > rev) rev = res.rev;
    epoch = res.epoch;
    for (const [key, r] of gone) if (r < rev) gone.delete(key);
    return touched;
  }
  // Один запрос за раз: кто пришёл во время запроса, ждёт его же
  function pull(full) {
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const res = await call("GET", full || rev === null ? "/api/sync/" : `/api/sync/?since=${rev}&epoch=${epoch}`);
        const touched = apply(res);
        if (touched.size) emit(touched);
        status(true);
      } catch (e) { status(false); throw e; }
      finally { inflight = null; }
    })();
    return inflight;
  }
  // Страница показывает «нет связи», если несколько опросов подряд не прошли
  function status(ok) {
    const was = fails >= 2; fails = ok ? 0 : fails + 1;
    if (was !== (fails >= 2)) window.dispatchEvent(new CustomEvent("crm-sync", { detail: { online: fails < 2 } }));
  }
  function start() {
    if (!ready) {
      ready = pull(true).then(() => {
        setInterval(() => { if (!document.hidden) pull().catch(e => console.warn("sync", e)); }, POLL_MS);
        document.addEventListener("visibilitychange", () => { if (!document.hidden) pull().catch(() => { }); });
      });
    }
    return ready;
  }

  // Ответ на свою запись тоже проходит проверку ревизии: если опрос уже принёс более свежую версию, она остаётся
  function put(c, id, res) {
    const cur = col(c).get(id);
    if (res.doc) { if (!cur || cur.rev <= res.doc.rev) col(c).set(id, { rev: res.doc.rev, data: res.doc.data }); }
    else if (cur && cur.rev <= res.rev) col(c).delete(id);
    emit(new Set([c]));
  }
  const newId = () => { const a = new Uint8Array(15); crypto.getRandomValues(a); return Array.from(a, b => "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"[b % 62]).join(""); };
  const url = (c, id) => `/api/db/${encodeURIComponent(c)}/${encodeURIComponent(id)}/`;
  const snapDoc = (c, id) => { const cur = col(c).get(id); return { id, exists: !!cur, data: () => cur ? clone(cur.data) : undefined }; };
  const docRef = (c, id) => ({
    id, path: c + "/" + id,
    get: async () => snapDoc(c, id),
    set: async data => put(c, id, await call("PUT", url(c, id), data)),
    update: async data => put(c, id, await call("PATCH", url(c, id), data)),
    delete: async () => { const res = await call("DELETE", url(c, id)); const cur = col(c).get(id); gone.set(c + "/" + id, res.rev); if (cur && cur.rev <= res.rev) col(c).delete(id); emit(new Set([c])); },
    onSnapshot: next => { const fn = () => next(snapDoc(c, id)); subs.push({ col: c, fn }); setTimeout(fn, 0); return () => { }; },
  });
  const colRef = c => ({
    path: c,
    doc: id => docRef(c, id || newId()),
    onSnapshot: next => {
      const fn = () => { const docs = [...col(c).keys()].sort().map(id => snapDoc(c, id)); next({ docs, size: docs.length, empty: !docs.length }); };
      subs.push({ col: c, fn }); setTimeout(fn, 0); return () => { };
    },
  });
  const db = { collection: colRef, doc: path => { const [c, id] = path.split("/"); return docRef(c, id); } };

  const user = {
    me: async () => me,
    can: async () => !!me.canWrite,
    profiles: async ids => { const list = [].concat(ids).filter(Boolean); return list.length ? call("GET", "/api/profiles/?ids=" + encodeURIComponent(list.join(","))) : {}; },
  };
  const downloads = {
    save: async ({ filename, data }) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([data], { type: "application/octet-stream" })); a.download = filename;
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      return { status: "saved" };
    },
  };

  const boot = call("GET", "/api/me/").then(m => { me = m; });
  window.claude = {
    use: async name => {
      await boot;
      if (name === "db") { await start(); return db; }
      return ({ user, downloads })[name] || null;
    },
  };
  // То, чего нет в Claude: учётные записи, выход, смена пароля
  window.crmServer = {
    accounts: {
      list: async () => (await call("GET", "/api/accounts/")).accounts,
      create: data => call("POST", "/api/accounts/", data),
      update: (id, data) => call("PATCH", `/api/accounts/${id}/`, data),
    },
    telegram: { link: () => call("POST", "/api/telegram/link/"), unlink: () => call("DELETE", "/api/telegram/link/") },
    logout: () => {
      const f = document.createElement("form"); f.method = "post"; f.action = "/logout/";
      const i = document.createElement("input"); i.type = "hidden"; i.name = "csrfmiddlewaretoken"; i.value = csrf();
      f.appendChild(i); document.body.appendChild(f); f.submit();
    },
    passwordUrl: "/password/", importUrl: "/backup/import/",
    fresh: () => pull(), // подтянуть чужие правки прямо сейчас, не дожидаясь очередного опроса
    __cache: () => Object.fromEntries(Object.entries(cache).flatMap(([c, m]) => [...m].map(([id, v]) => [c + "/" + id, v.data]))),
  };
})();
