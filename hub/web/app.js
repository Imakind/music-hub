// Веб-плеер Hub: поиск по каталогу, страницы артиста/альбома, очередь и воспроизведение.
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const audio = $("#audio");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (s) => { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { location.reload(); throw new Error("требуется вход"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg; document.body.append(t);
  setTimeout(() => t.remove(), 3500);
}

// ---------------- навигация ----------------
const history = [];
let current = null;

function go(route, push = true) {
  if (push && current) history.push(current);
  current = route;
  $("#back").hidden = history.length === 0;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === route.tab));
  window.scrollTo(0, 0);
  render(route);
}
$("#back").onclick = () => { if (history.length) go(history.pop(), false); };
document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => {
  const tab = b.dataset.tab;
  if (tab === "search") go({ tab, name: "search", q: $("#q").value });
  else go({ tab, name: tab });
}));
$("#search-form").onsubmit = (e) => { e.preventDefault(); $("#q").blur(); go({ tab: "search", name: "search", q: $("#q").value }); };
let typing;
$("#q").oninput = () => { clearTimeout(typing); typing = setTimeout(() => go({ tab: "search", name: "search", q: $("#q").value }, current?.name !== "search"), 450); };

async function render(r) {
  const token = (render.token = {});
  view.innerHTML = `<div class="spinner"></div>`;
  try {
    let html;
    if (r.name === "home") html = await renderHome();
    else if (r.name === "search") html = await renderSearch(r.q);
    else if (r.name === "artist") html = await renderArtist(r.id);
    else if (r.name === "album") html = await renderAlbum(r.id);
    else if (r.name === "library") html = await renderLibrary();
    else if (r.name === "queue") html = renderQueue();
    if (token !== render.token) return;
    view.innerHTML = html;
    bindView();
  } catch (e) {
    if (token === render.token) view.innerHTML = `<div class="empty">Ошибка: ${esc(e.message)}</div>`;
  }
}

// списки треков, отрисованные на экране: id списка → массив треков
let lists = {};
function trackList(tracks, { numbered = false, listId } = {}) {
  listId = listId || `l${Object.keys(lists).length}`;
  lists[listId] = tracks;
  if (!tracks.length) return `<div class="empty">Пусто</div>`;
  return `<div class="tracks">${tracks.map((t, i) => `
    <div class="track ${playingId() === t.id ? "playing" : ""}" data-list="${listId}" data-i="${i}" data-id="${t.id}">
      ${numbered ? `<div class="num">${t.track_position || i + 1}</div>` : `<img loading="lazy" src="${esc(t.album?.cover_small)}" alt="">`}
      <div class="t-main">
        <div class="t-title">${esc(t.title)}</div>
        <div class="t-sub">${t.explicit ? `<span class="badge-e">E</span>` : ""}${esc(t.artist?.name)}${numbered ? "" : ` · ${esc(t.album?.title)}`}</div>
      </div>
      <span class="dot ${dotClass(t.status)}" title="${esc(statusText(t.status))}"></span>
      <span class="dur">${fmt(t.duration)}</span>
      <button class="icon-btn more" data-src="${t.id}" aria-label="Источники"><svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg></button>
    </div>`).join("")}</div>`;
}
const dotClass = (s) => (s === "ready" ? "ready" : s === "error" ? "error" : ["queued", "resolving", "downloading"].includes(s) ? "busy" : "");
const statusText = (s) => ({ ready: "В библиотеке", error: "Ошибка", queued: "В очереди", resolving: "Ищу источник", downloading: "Скачиваю", resolved: "Источник найден" }[s] || "Не скачан");

const albumCard = (a) => `<button class="card" data-album="${a.id}"><img loading="lazy" src="${esc(a.cover_small)}" alt="">
  <div class="c-title">${esc(a.title)}</div><div class="c-sub">${esc([a.year, a.artist?.name].filter(Boolean).join(" · "))}</div></button>`;
const artistCard = (a) => `<button class="card round" data-artist="${a.id}"><img loading="lazy" src="${esc(a.picture_small)}" alt="">
  <div class="c-title">${esc(a.name)}</div><div class="c-sub">Исполнитель</div></button>`;

let mixPoll;
async function renderHome() {
  lists = {};
  clearTimeout(mixPoll);
  const [{ mix, state }, prof, wk] = await Promise.all([api("/api/mix/daily"), api("/api/profile"), api("/api/worker").catch(() => null)]);
  const jobs = wk ? Object.values(wk.pending).reduce((a, b) => a + b, 0) : 0;
  const workerHtml = wk && wk.mode === "server" ? `<div class="muted" style="font-size:12px">Домашний компьютер: ${wk.online ? "на связи" : "выключен"}${jobs ? ` · в очереди задач: ${jobs}` : ""}</div>` : "";
  if (state.running) mixPoll = setTimeout(() => current?.name === "home" && render(current), 3000);
  const today = new Date().toISOString().slice(0, 10);
  let mixHtml;
  if (mix?.tracks?.length) {
    const covers = [...new Set(mix.tracks.map((t) => t.album?.cover_small).filter(Boolean))].slice(0, 8);
    const ready = mix.tracks.filter((t) => t.status === "ready").length;
    mixHtml = `<div class="collage">${covers.map((c) => `<img src="${esc(c)}" alt="">`).join("")}</div>
      <div class="muted">${mix.tracks.length} треков · ${mix.stats.familiar} знакомых, ${mix.stats.new} новых · скачано ${ready}
      ${mix.date !== today ? " · вчерашний" : ""}</div>
      <div class="actions"><button class="btn primary" data-playlist="mix">Слушать</button>
      <button class="btn" data-shuffle="mix">Перемешать</button>
      <button class="btn" id="mix-rebuild" ${state.running ? "disabled" : ""}>${state.running ? esc(state.stage) + "…" : "Пересобрать"}</button></div>`;
    lists.mix = mix.tracks;
  } else {
    mixHtml = `<div class="muted">${state.running ? esc(state.stage) + "…" : state.error ? "Ошибка: " + esc(state.error)
      : "Собирается каждое утро из 50 треков: 40% знакомого, 60% нового."}</div>
      <div class="actions"><button class="btn primary" id="mix-rebuild" ${state.running ? "disabled" : ""}>Собрать сейчас</button></div>`;
  }
  const emb = prof.embedder;
  return `<div class="home-grid">
    <div class="panel wave-hero"><h3>Моя волна</h3>
      <div>Бесконечный поток под ваш вкус. Подстраивается под пропуски и лайки прямо во время прослушивания.</div>
      <div class="actions"><button class="btn" style="background:#fff;color:#000" id="wave-me">▶ Запустить</button></div></div>
    <div class="panel"><h3>Микс дня</h3>${mixHtml}</div>
    <div class="panel"><h3>Профиль вкуса</h3>
      <div class="muted">${prof.events} действий · ${prof.liked} ♥ · ${prof.disliked} ✕ · групп вкуса: ${prof.clusters.length || "—"}</div>
      <div class="chips">${prof.top_artists.filter((a) => a.weight > 0).map((a) => `<span class="chip">${esc(a.name)}</span>`).join("") || `<span class="muted">Послушайте несколько треков</span>`}</div>
      <div class="muted" style="font-size:12px">Анализ звучания: ${emb.enabled ? `${esc(emb.model)}${emb.device ? " · " + esc(emb.device) : ""}` : "выключен — " + esc(emb.reason || "")}</div>
      ${workerHtml}
    </div></div>
    ${mix?.tracks?.length ? `<h2>Микс дня</h2>${trackList(mix.tracks, { listId: "mix" })}` : ""}`;
}

async function renderSearch(q) {
  lists = {};
  if (!q || !q.trim()) return `<div class="empty">Найдите любого исполнителя, альбом или трек.<br>Трек скачается при первом запуске.</div>`;
  const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
  let h = "";
  if (r.artists.length) h += `<h2>Исполнители</h2><div class="row-scroll">${r.artists.map(artistCard).join("")}</div>`;
  if (r.tracks.length) h += `<h2>Треки</h2>${trackList(r.tracks)}`;
  if (r.albums.length) h += `<h2>Альбомы</h2><div class="row-scroll">${r.albums.map(albumCard).join("")}</div>`;
  return h || `<div class="empty">Ничего не найдено</div>`;
}

async function renderArtist(id) {
  lists = {};
  const a = await api(`/api/artist/${id}`);
  const groups = { album: "Альбомы", ep: "EP", single: "Синглы", compile: "Сборники" };
  let h = `<div class="hero round"><img src="${esc(a.picture)}" alt=""><div><div class="muted">Исполнитель</div><h1>${esc(a.name)}</h1>
    ${a.fans ? `<div class="muted">${a.fans.toLocaleString("ru")} слушателей в Deezer</div>` : ""}</div></div>
    <div class="actions"><button class="btn primary" data-playlist="top">Слушать популярное</button>
    <button class="btn" data-wave-artist="${a.id}">Моя волна по исполнителю</button></div>
    <h2>Популярное</h2>${trackList(a.top, { listId: "top" })}`;
  for (const [type, title] of Object.entries(groups)) {
    const al = a.albums.filter((x) => x.type === type);
    if (al.length) h += `<h2>${title}</h2><div class="grid">${al.map(albumCard).join("")}</div>`;
  }
  return h;
}

async function renderAlbum(id) {
  lists = {};
  const a = await api(`/api/album/${id}`);
  const total = a.tracks.reduce((s, t) => s + t.duration, 0);
  return `<div class="hero"><img src="${esc(a.cover)}" alt=""><div><div class="muted">${esc(a.type === "single" ? "Сингл" : "Альбом")}${a.explicit ? " · Explicit" : ""}</div>
    <h1>${esc(a.title)}</h1><div><button class="link" data-artist="${a.artist?.id}"><b>${esc(a.artist?.name)}</b></button>
    <span class="muted"> · ${esc(a.year)} · ${a.tracks.length} тр. · ${Math.round(total / 60)} мин</span></div></div></div>
    <div class="actions"><button class="btn primary" data-playlist="album">Слушать</button>
    <button class="btn" data-download="album">Скачать альбом</button></div>
    ${trackList(a.tracks, { numbered: true, listId: "album" })}`;
}

async function renderLibrary() {
  lists = {};
  const r = await api("/api/library");
  return `<h2>Моя музыка</h2>${r.tracks.length ? `<div class="actions"><button class="btn primary" data-playlist="lib">Слушать всё</button>
    <button class="btn" data-shuffle="lib">Перемешать</button></div>` : ""}${trackList(r.tracks, { listId: "lib" })}`;
}

function renderQueue() {
  lists = {};
  if (!queue.length) return `<div class="empty">Очередь пуста</div>`;
  return `<h2>Очередь</h2>${wave ? `<div class="wave-banner">〰 Моя волна: новые треки подбираются по ходу прослушивания</div>` : ""}
    ${trackList(queue, { listId: "queue" })}`;
}

function bindView() {
  view.querySelectorAll("[data-artist]").forEach((b) => (b.onclick = () => go({ tab: current.tab, name: "artist", id: b.dataset.artist })));
  view.querySelectorAll("[data-album]").forEach((b) => (b.onclick = () => go({ tab: current.tab, name: "album", id: b.dataset.album })));
  view.querySelectorAll(".track").forEach((el) => (el.onclick = (e) => {
    if (e.target.closest("[data-src]")) return;
    const list = lists[el.dataset.list];
    if (el.dataset.list === "queue") playIndex(+el.dataset.i);
    else setQueue(list, +el.dataset.i);
  }));
  view.querySelectorAll("[data-src]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); openSources(+b.dataset.src); }));
  view.querySelectorAll("[data-playlist]").forEach((b) => (b.onclick = () => setQueue(lists[b.dataset.playlist], 0)));
  view.querySelectorAll("[data-shuffle]").forEach((b) => (b.onclick = () => {
    const l = [...lists[b.dataset.shuffle]].sort(() => Math.random() - 0.5); setQueue(l, 0);
  }));
  view.querySelectorAll("[data-wave-artist]").forEach((b) => (b.onclick = () => startWave({ artist_id: +b.dataset.waveArtist })));
  const wm = $("#wave-me", view); if (wm) wm.onclick = () => startWave({});
  const rb = $("#mix-rebuild", view); if (rb) rb.onclick = async () => {
    rb.disabled = true;
    await api("/api/mix/daily/rebuild", { method: "POST" }).catch((e) => toast(e.message));
    render(current);
  };
  view.querySelectorAll("[data-download]").forEach((b) => (b.onclick = async () => {
    const l = lists[b.dataset.download];
    for (const t of l) { if (t.status !== "ready") { api(`/api/tracks/${t.id}/fetch`, { method: "POST" }).catch(() => {}); watch(t.id); } }
    toast(`Скачиваю ${l.length} треков`);
  }));
  // заранее подобрать источники для видимых треков
  const ids = Object.values(lists).flat().filter((t) => !t.status).map((t) => t.id);
  if (ids.length) api("/api/prefetch", { method: "POST", body: JSON.stringify({ ids: [...new Set(ids)].slice(0, 30) }) }).catch(() => {});
}

function updateDots(id, status) {
  document.querySelectorAll(`.track[data-id="${id}"] .dot`).forEach((d) => { d.className = `dot ${dotClass(status)}`; d.title = statusText(status); });
  for (const l of [...Object.values(lists), queue]) for (const t of l) if (t.id === id) t.status = status;
}

// следим за статусом скачивания
const watchers = new Map();
function watch(id) {
  if (watchers.has(id)) return watchers.get(id);
  const p = new Promise((resolve) => {
    const tick = async () => {
      try {
        const s = await api(`/api/tracks/${id}`);
        updateDots(id, s.status);
        if (id === playingId()) setState(statusText(s.status) + (s.status === "ready" ? "" : "…"));
        if (s.status === "ready" || s.status === "error" || !s.status) return resolve(s);
      } catch {}
      setTimeout(tick, 1000);
    };
    tick();
  }).finally(() => watchers.delete(id));
  watchers.set(id, p);
  return p;
}

// ---------------- плеер ----------------
let queue = store.get("queue", []);
let qi = store.get("qi", 0);
let wave = store.get("wave", null); // id сессии «Моей волны» или null
const playingId = () => queue[qi]?.id;
const saveQ = () => { store.set("queue", queue); store.set("qi", qi); store.set("wave", wave); };
const setState = (s) => ($("#p-state").textContent = s);

function setQueue(list, i, waveId = null) {
  leaveCurrent();
  wave = waveId;
  $("#p-wave").classList.toggle("on", !!wave);
  queue = list.map((t) => ({ ...t }));
  playIndex(i);
}

// ---------- действия для рекомендаций ----------
// +2 дослушал, +1 лайк, −1 пропуск в первые 30 с, −5 «не нравится»
let session = null; // { id, started, ended }
function sendEvent(id, type, position) {
  return api("/api/events", { method: "POST", body: JSON.stringify({ track_id: id, type, position, wave }) }).catch(() => null);
}
function leaveCurrent() {
  if (session && !session.ended && session.started) {
    const pos = audio.currentTime;
    if (pos < 30) sendEvent(session.id, "skip", pos);
    else if (audio.duration && pos / audio.duration > 0.9) sendEvent(session.id, "complete", pos);
  }
  session = null;
}

async function startWave(body) {
  toast("Запускаю волну…");
  try {
    const r = await api("/api/wave/start", { method: "POST", body: JSON.stringify(body) });
    if (!r.tracks.length) return toast("Не удалось подобрать треки");
    const cur = queue[qi];
    if (cur && r.tracks[0].id === cur.id && audio.src && !audio.paused) {
      // волна от играющего трека: не перезапускаем его, просто меняем очередь
      wave = r.id; queue = [cur, ...r.tracks.slice(1)]; qi = 0; saveQ();
      $("#p-wave").classList.add("on");
      toast("Моя волна: дальше — похожие треки");
      if (current?.name === "queue") render(current);
      prepareNext();
    } else setQueue(r.tracks, 0, r.id);
  } catch (e) { toast(e.message); }
}
let waveLoading = false;
async function topUpWave() {
  if (!wave || waveLoading || qi < queue.length - 2) return;
  waveLoading = true;
  try {
    const r = await api(`/api/wave/${wave}/next?n=3`);
    const have = new Set(queue.map((t) => t.id));
    queue.push(...r.tracks.filter((t) => !have.has(t.id)));
    saveQ();
    if (current?.name === "queue") render(current);
  } catch (e) {
    if (String(e.message).includes("устарела")) { wave = null; $("#p-wave").classList.remove("on"); saveQ(); }
  } finally { waveLoading = false; }
}

function syncRating() {
  const t = queue[qi];
  $("#p-like").classList.toggle("on", t?.rating === 1);
  $("#p-dislike").classList.toggle("on", t?.rating === -1);
}
$("#p-like").onclick = async () => {
  const t = queue[qi]; if (!t) return;
  const r = await sendEvent(t.id, t.rating === 1 ? "unlike" : "like", audio.currentTime);
  t.rating = r?.rating ?? (t.rating === 1 ? 0 : 1); saveQ(); syncRating();
  if (t.rating === 1) toast("Добавлено в любимые");
};
$("#p-dislike").onclick = async () => {
  const t = queue[qi]; if (!t) return;
  if (t.rating === -1) { await sendEvent(t.id, "undislike"); t.rating = 0; syncRating(); return; }
  await sendEvent(t.id, "dislike", audio.currentTime);
  t.rating = -1; session = null; // не считать это ещё и пропуском
  toast("Больше не будет в рекомендациях");
  playIndex(qi + 1);
};
$("#p-wave").onclick = () => { const t = queue[qi]; if (t) startWave({ track_id: t.id }); };

async function ensureReady(t) {
  if (t.status === "ready") return true;
  const r = await api(`/api/tracks/${t.id}/fetch`, { method: "POST" });
  updateDots(t.id, r.status);
  if (r.status === "ready") return true;
  // на сервере скачивает домашний компьютер: если он выключен, трек ждёт в очереди
  const w = await api("/api/worker").catch(() => null);
  if (w && !w.online) {
    const e = new Error("домашний компьютер выключен, трек скачается, когда он включится");
    e.offline = true;
    throw e;
  }
  const s = await watch(t.id);
  if (s.status === "error") throw new Error(s.error || "не удалось скачать");
  return s.status === "ready";
}

async function playIndex(i) {
  if (i < 0 || i >= queue.length) return;
  if (session && session.id !== queue[i]?.id) leaveCurrent();
  qi = i; saveQ();
  const t = queue[i];
  session = { id: t.id, started: false, ended: false };
  showTrack(t);
  syncRating();
  topUpWave();
  audio.pause();
  setState("Подготовка…");
  try {
    await ensureReady(t);
    if (playingId() !== t.id) return; // пользователь уже переключил
    audio.src = `/api/stream/${t.id}`;
    await audio.play();
    if (session?.id === t.id) { session.started = true; sendEvent(t.id, "play", 0); }
    setState("");
    prepareNext();
  } catch (e) {
    if (playingId() !== t.id) return;
    if (e.offline) {
      // переходим к следующему уже скачанному треку, иначе останавливаемся
      const j = queue.findIndex((x, k) => k > i && x.status === "ready");
      setState("Ждёт домашний компьютер");
      toast(`«${t.title}»: ${e.message}.`);
      if (j >= 0) setTimeout(() => playingId() === t.id && playIndex(j), 1500);
      return;
    }
    setState("Ошибка");
    toast(`«${t.title}»: ${e.message}. Пропускаю.`);
    setTimeout(() => playingId() === t.id && playIndex(i + 1), 1200);
  }
}

function prepareNext() {
  const n = queue[qi + 1];
  if (n && n.status !== "ready") api(`/api/tracks/${n.id}/fetch`, { method: "POST" }).then(() => watch(n.id)).catch(() => {});
}

function showTrack(t) {
  $("#player").hidden = false;
  $("#p-cover").src = t.album?.cover_small || "";
  $("#p-title").textContent = t.title;
  $("#p-artist").textContent = t.artist?.name || "";
  document.querySelectorAll(".track").forEach((el) => el.classList.toggle("playing", +el.dataset.id === t.id));
  document.title = `${t.title} — ${t.artist?.name}`;
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title, artist: t.artist?.name, album: t.album?.title,
      artwork: t.album?.cover ? [{ src: t.album.cover, sizes: "1000x1000", type: "image/jpeg" }] : [],
    });
  }
}

const PLAY = `<path d="M7 4v16l13-8z"/>`, PAUSE = `<path d="M6 4h4v16H6zM14 4h4v16h-4z"/>`;
const syncPlay = () => { $("#p-play-ico").innerHTML = audio.paused ? PLAY : PAUSE; };
syncPlay();
audio.onplay = audio.onpause = syncPlay;
audio.onended = () => {
  if (session && !session.ended) { session.ended = true; sendEvent(session.id, "complete", audio.currentTime); }
  playIndex(qi + 1);
};
audio.ontimeupdate = () => {
  $("#t-cur").textContent = fmt(audio.currentTime);
  $("#t-dur").textContent = fmt(audio.duration || queue[qi]?.duration);
  $("#progress-fill").style.width = `${(audio.currentTime / (audio.duration || 1)) * 100}%`;
};
$("#p-play").onclick = () => {
  if (!audio.src && queue[qi]) return playIndex(qi);
  audio.paused ? audio.play() : audio.pause();
};
$("#p-next").onclick = () => playIndex(qi + 1);
$("#p-prev").onclick = () => (audio.currentTime > 3 ? (audio.currentTime = 0) : playIndex(qi - 1));
$("#progress").onclick = (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  if (audio.duration) audio.currentTime = ((e.clientX - r.left) / r.width) * audio.duration;
};
if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("play", () => audio.play());
  navigator.mediaSession.setActionHandler("pause", () => audio.pause());
  navigator.mediaSession.setActionHandler("nexttrack", () => playIndex(qi + 1));
  navigator.mediaSession.setActionHandler("previoustrack", () => playIndex(qi - 1));
  navigator.mediaSession.setActionHandler("seekto", (d) => (audio.currentTime = d.seekTime));
}
if (queue[qi]) { showTrack(queue[qi]); syncRating(); }
$("#p-wave").classList.toggle("on", !!wave);

// ---------------- выбор источника ----------------
let srcTrack = null;
async function openSources(id, refresh = false) {
  srcTrack = id;
  const dlg = $("#sources");
  if (!dlg.open) dlg.showModal();
  $("#src-list").innerHTML = `<div class="spinner"></div>`;
  try {
    const r = await api(`/api/tracks/${id}/candidates${refresh ? "?refresh=1" : ""}`);
    if (!r.candidates.length && !r.worker_online) {
      $("#src-list").innerHTML = `<div class="empty">Домашний компьютер выключен.<br>Источники появятся, когда он включится.</div>`;
      return;
    }
    $("#src-list").innerHTML = r.candidates.length ? r.candidates.map((c) => `
      <div class="src ${c.video_id === r.chosen ? "chosen" : ""}">
        <div class="s-score">${Math.round(c.score)}</div>
        <div class="s-main">
          <div class="s-title">${esc(c.title)}</div>
          <div class="s-sub">${esc(c.channel)} · ${fmt(c.duration)} · ${c.kind === "song" ? "YT Music" : "YouTube"}${c.is_explicit ? " · Explicit" : ""}</div>
          ${c.reasons.length ? `<div class="s-sub">${esc(c.reasons.join(", "))}</div>` : ""}
          <a href="${esc(c.url)}" target="_blank" rel="noopener">Открыть на YouTube</a>
        </div>
        <button class="btn" data-use="${esc(c.video_id)}">${c.video_id === r.chosen ? "Перекачать" : "Использовать"}</button>
      </div>`).join("") : `<div class="empty">Ничего не нашлось</div>`;
    $("#src-list").querySelectorAll("[data-use]").forEach((b) => (b.onclick = async () => {
      await api(`/api/tracks/${id}/source`, { method: "POST", body: JSON.stringify({ video_id: b.dataset.use }) });
      dlg.close();
      toast("Перекачиваю из выбранного источника");
      updateDots(id, "queued");
      const s = await watch(id);
      if (s.status === "ready" && playingId() === id) {
        const pos = audio.currentTime;
        audio.src = `/api/stream/${id}?v=${Date.now()}`;
        audio.currentTime = pos; audio.play();
      }
    }));
  } catch (e) {
    $("#src-list").innerHTML = `<div class="empty">Ошибка: ${esc(e.message)}</div>`;
  }
}
$("#src-refresh").onclick = () => srcTrack && openSources(srcTrack, true);

// ---------------- старт ----------------
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
go({ tab: "home", name: "home" }, false);
