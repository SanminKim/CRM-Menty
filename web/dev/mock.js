// Мини-эмуляция window.claude для локальной проверки страницы
(function(){
  const role = (new URLSearchParams(location.search)).get('role') || 'owner';
  const store = window.__store = JSON.parse(localStorage.getItem('mockstore')||'{}');
  const persist = () => localStorage.setItem('mockstore', JSON.stringify(store));
  const subs = [];
  let n = 0; const gen = () => 'id' + Date.now().toString(36) + (n++).toString(36);
  const visible = path => role !== 'partner' || path.startsWith('reports/');
  const snapDoc = (path) => ({ id: path.split('/').pop(), exists: path in store && visible(path), data: () => store[path] ? JSON.parse(JSON.stringify(store[path])) : undefined, metadata:{fromCache:false,hasPendingWrites:false} });
  const notify = () => setTimeout(() => subs.forEach(s => s()), 5);
  const canWrite = role !== 'partner';
  const docRef = path => ({
    id: path.split('/').pop(), path,
    get: async () => snapDoc(path),
    set: async d => { if(!canWrite) throw {code:'invalid_argument'}; store[path] = JSON.parse(JSON.stringify(d)); persist(); notify(); },
    update: async d => { if(!canWrite) throw {code:'invalid_argument'}; if(!(path in store)) throw {code:'invalid_argument'}; Object.assign(store[path], JSON.parse(JSON.stringify(d))); persist(); notify(); },
    delete: async () => { delete store[path]; persist(); notify(); },
    onSnapshot: (next) => { const f = () => next(snapDoc(path)); subs.push(f); setTimeout(f, 10); return () => {}; },
  });
  const colRef = path => ({
    path,
    doc: id => docRef(path + '/' + (id || gen())),
    onSnapshot: (next) => {
      const f = () => { const docs = Object.keys(store).filter(k => k.startsWith(path + '/') && k.split('/').length === path.split('/').length + 1 && visible(k)).sort().map(snapDoc); next({ docs, size: docs.length, empty: !docs.length, docChanges: () => [], metadata:{fromCache:false,hasPendingWrites:false} }); };
      subs.push(f); setTimeout(f, 15); return () => {};
    },
  });
  const db = { doc: docRef, collection: colRef };
  const user = {
    me: async () => ({ id: role === 'partner' ? 'u_blogger' : 'u_owner', isOwner: role === 'owner', name: '', email: null, canEdit: role==='owner' }),
    can: async () => role === 'partner' ? false : true,
    profiles: async ids => Object.fromEntries([].concat(ids).map(i => [i, { id: i, name: i === 'u_owner' ? 'Владелец' : '' }])),
  };
  const downloads = { save: async ({filename, data}) => { window.__saved = {filename, size: data.length}; return {status:'saved'}; } };
  window.claude = { use: async name => ({ db, user, downloads })[name] || null };
})();
