import {$, el, sv, css, clamp, fmtLap, fx, signed, when, fmtStamp, api, LANGS, LANG, t, loadStrings, UNIT, setShortTracks, shortTrack, className, carName, showError, S, LA, pills, refreshThumbs, stintNo, racingLaps, lapKey, parseKey, bestLap, keyOk, bestOverall, compare, timeAt, lapT, readK} from "./state.js";
import {buildGeo, fitNow, initMap} from "./map.js";
import {buildDock, setPlay, initTraces, initAnalysisLoop} from "./traces.js";
import {loadProposal, renderSetup} from "./setup.js";

// ---------- theme ----------
const LIGHT = matchMedia("(prefers-color-scheme: light)");
function themePref() { try { return localStorage.getItem("autosetup.theme") || "dark"; } catch (e) { return "dark"; } }
function setThemePref(p) {
 try { localStorage.setItem("autosetup.theme", p); } catch (e) {}
 document.documentElement.dataset.theme = p === "system" ? (LIGHT.matches ? "light" : "dark") : p;
 redraw();
}
LIGHT.addEventListener("change", () => { if (themePref() === "system") setThemePref("system"); });

const SCHEMES = ["red", "violet", "plum", "iris"];
function schemePref() { try { const s = localStorage.getItem("autosetup.scheme"); return SCHEMES.includes(s) ? s : "red"; } catch (e) { return "red"; } }
function setSchemePref(s) {
 try { localStorage.setItem("autosetup.scheme", s); } catch (e) {}
 if (s === "red") delete document.documentElement.dataset.scheme; else document.documentElement.dataset.scheme = s;
 redraw();
}

// ---------- screens and nav ----------
function show(screen) {
 S.screen = screen;
 for (const s of ["home", "analysis", "setup", "history", "driver", "settings"]) $("screen-" + s).classList.toggle("on", s === screen);
 for (const b of document.querySelectorAll(".tab")) b.classList.toggle("on", b.dataset.screen === screen);
 renderNav();
 if (screen === "home") loadHome();
 if (screen === "analysis") setTimeout(() => { renderAnalysisBar(); drawAnalysis(); }, 0);
 if (screen === "setup") { if (!S.proposal) loadProposal(); renderSetup(); }
 if (screen === "history") loadHistory();
 if (screen === "driver") loadDriver();
 if (screen === "settings") loadSettings();
 requestAnimationFrame(refreshThumbs);
 window.scrollTo(0, 0);
}

for (const b of document.querySelectorAll(".tab")) b.onclick = () => { if (!b.disabled) show(b.dataset.screen); };

function renderNav() {
 const has = !!S.session, o = S.live;
 $("tab-analysis").disabled = !has && o.state !== "live";
 $("tab-setup").disabled = !has;
 const s = S.session, track = s ? s.layout : o.state === "live" ? o.track || "" : "", car = s ? s.car_name : o.state === "live" ? o.vehicle || "" : "";
 $("ctx").textContent = track ? `${shortTrack(track)} · ${car}` : "";
 $("a-track").textContent = shortTrack(track); $("a-car").textContent = car;
 $("a-model").textContent = s && s.model ? carName(s.model) : "";
 $("live").classList.toggle("on", o.state === "live");
 $("live-text").textContent = o.state === "live" ? t("nav.live", {session: o.session_name || o.track || ""}) : o.state === "menu" ? t("nav.menu_open") : t("nav.off");
 $("hist-back").hidden = !(S.history && o.state === "live");
}

$("hist-back").onclick = async () => {
 S.history = false;
 if (S.live.state === "live" && S.live.session) { await openSession(S.live.session, false); show("analysis"); }
 else { S.session = null; S.stints = []; renderNav(); show("home"); }
};

function openMenu() { $("menu").classList.add("open"); $("menu-btn").setAttribute("aria-expanded", "true"); }
function closeMenu() { $("menu").classList.remove("open"); $("menu-btn").setAttribute("aria-expanded", "false"); }
$("menu-btn").onclick = e => { e.stopPropagation(); $("menu").classList.contains("open") ? closeMenu() : openMenu(); };
document.addEventListener("click", e => { if (!e.target.closest(".menu-wrap")) closeMenu(); });
for (const b of document.querySelectorAll("[data-go]")) b.onclick = () => { closeMenu(); if (b.dataset.go === "about") openAbout(); else show(b.dataset.go); };
document.addEventListener("keydown", e => {
 if (e.key === "Escape") { if ($("menu").classList.contains("open")) closeMenu(); else if (!$("about").hidden) $("about").hidden = true; return; }
 if (e.code === "Space" && S.screen === "analysis" && !/INPUT|BUTTON|TEXTAREA/.test(e.target.tagName)) { e.preventDefault(); setPlay(!S.play); }
});

// ---------- tooltips ----------
const tip = $("tip");
document.addEventListener("mouseover", e => { const x = e.target.closest("[data-tip]"); if (!x) { tip.style.display = "none"; return; } tip.textContent = x.getAttribute("data-tip"); tip.style.display = "block"; });
document.addEventListener("mousemove", e => { if (tip.style.display !== "block") return; tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8) + "px"; tip.style.top = Math.min(e.clientY + 14, innerHeight - tip.offsetHeight - 8) + "px"; });

// ---------- sessions ----------
async function openSession(id, history) {
 const sessions = (await api("/api/sessions")).sessions;
 S.session = sessions.find(s => s.id === id) || null;
 S.history = history && !!S.session;
 S.a = S.b = null; S.proposal = null; S.open = new Set(); S.t = 0; setPlay(false); S.follow = false;
 renderNav();
 await loadStints(false);
}

let stintsRun = null, stintsQueued = null;
function loadStints(keep = true) {
 if (stintsRun) { stintsQueued = stintsQueued === null ? keep : stintsQueued && keep; return stintsRun; }
 stintsRun = (async () => { try { await loadStintsNow(keep); } finally { stintsRun = null; if (stintsQueued !== null) { const k = stintsQueued; stintsQueued = null; loadStints(k); } } })();
 return stintsRun;
}

async function loadStintsNow(keep) {
 if (!S.session) { S.stints = []; renderStints(); drawAnalysis(); return; }
 const before = S.stints.length;
 S.stints = (await api(`/api/stints?session=${S.session.id}`)).stints;
 if (!keep || S.stints.length !== before) { if (!keep) { S.a = null; S.b = null; } }
 S.proposal = null;
 await loadMap();
 pickDefaults();
 renderStints();
 await loadLaps();
 if (S.screen === "setup") loadProposal();
}

function pickDefaults() {
 if (!keyOk(S.a)) { const st = [...S.stints].reverse().find(s => bestLap(s)) || null, l = st && bestLap(st); S.a = l ? lapKey(st.id, l.seq) : null; }
 if (S.b && (!keyOk(S.b) || S.b === S.a)) S.b = null;
 if (S.a) S.open.add(parseKey(S.a).stint);
 if (S.b) S.open.add(parseKey(S.b).stint);
}

function pickLap(k) {
 if (k === S.a) { S.a = S.b; S.b = null; }
 else if (k === S.b) S.b = null;
 else if (!S.a) S.a = k;
 else S.b = k;
 renderStints(); loadLaps();
}

function renderStints() {
 const g = $("stints");
 if (!S.stints.length) { g.replaceChildren(el("div", {class: "st"}, "")); g.firstChild.append(el("div", {class: "empty"}, t("stints.none"))); return; }
 const best = bestOverall();
 g.replaceChildren(...[...S.stints].reverse().map(st => {
  const laps = racingLaps(st), div = el("div", {class: "st"});
  if (!laps.length) { div.classList.add("nolap"); const h = el("div", {class: "sh"}); h.append(el("span", {class: "n"}, t("stint.n", {n: stintNo(st)})), el("span", {class: "sub"}, t("stint.nolap"))); div.append(h); return div; }
  const bl = bestLap(st), open = S.open.has(st.id), isBest = best && best.s === st;
  if (open) div.classList.add("open");
  const h = el("button", {type: "button", class: "sh", "aria-expanded": open});
  h.append(el("span", {class: "n"}, t("stint.n", {n: stintNo(st)})), el("span", {class: "bt" + (isBest ? " best" : "")}, fmtLap(bl && bl.time)),
   el("span", {class: "sub"}, t(laps.length === 1 ? "stint.lap" : "stint.laps", {n: laps.length})), el("span", {class: "sub"}, isBest ? t("stint.best") : ""));
  h.onclick = () => { if (S.open.has(st.id)) S.open.delete(st.id); else S.open.add(st.id); renderStints(); };
  div.append(h);
  if (open) {
   const body = el("div", {class: "body"});
   for (const l of laps) {
    const k = lapKey(st.id, l.seq), r = el("button", {type: "button", class: "lr" + (k === S.a ? " isA" : "") + (k === S.b ? " isB" : ""), "aria-pressed": k === S.a || k === S.b});
    const name = el("span", {}, t("lap.n", {n: l.lap}));
    if (!l.clean) name.append(el("span", {class: "x"}, l.off_track ? t("lap.offtrack") : t("lap.notclean")));
    // A and B only while two laps are compared; one lap open is just a filled dot
    const mark = !(S.a && S.b) ? el("span", {class: "dot" + (k === S.a ? " on" : "")}) : k === S.a ? el("span", {class: "ab a"}, "A") : k === S.b ? el("span", {class: "ab b"}, "B") : el("span", {class: "dot"});
    r.append(mark, name, el("span", {class: l.clean ? "" : "bad"}, fmtLap(l.time)));
    r.onclick = () => pickLap(k);
    body.append(r);
   }
   div.append(body);
  }
  return div;
 }));
}

async function loadMap() {
 if (!S.session) { S.map = null; return; }
 if (S.mapCtx === S.session.context && S.map) return;
 try { const r = await api("/api/trackmap?context=" + encodeURIComponent(S.session.context)); S.map = r.map; S.mapCtx = S.session.context; } catch (e) { S.map = null; }
}

const lapCache = new Map();
async function getLap(k) {
 if (!k) return null;
 if (lapCache.has(k)) return lapCache.get(k);
 const {stint, seq} = parseKey(k);
 try { const l = await api(`/api/lap?stint=${stint}&seq=${seq}`); lapCache.set(k, l); return l; } catch (e) { showError(t("err.lap", {e: e.message})); return null; }
}

let lapsSeq = 0;
async function loadLaps() {
 const my = ++lapsSeq;
 const [a, b] = await Promise.all([getLap(S.a), getLap(S.b)]);
 if (my !== lapsSeq) return;  // a newer pick is already loading
 S.lapA = a; S.lapB = b;
 S.delta = a && b ? a.trace.distance.map((d, i) => a.trace.time[i] == null ? null : a.trace.time[i] - timeAt(b, d)) : null;
 S.t = clamp(S.t, 0, lapT());
 buildGeo(); if (!S.follow) fitNow();
 renderAnalysisBar(); drawAnalysis();
}

function renderAnalysisBar() {
 if (!compare() && S.line === "delta") S.line = "pedals";
 pills("linemode", ["pedals", "delta"], [t("line.pedals"), t("line.delta")], S.line, v => { S.line = v; LA.dirty = true; });
 $("linemode").querySelectorAll("button")[1].disabled = !compare();
 const g = $("agap"), d = compare() ? S.lapA.time - S.lapB.time : null;
 g.textContent = d == null ? "" : `A ${signed(d, 3)} s ${t("gap.tob")}`; g.style.color = d < 0 ? "var(--green)" : "var(--red)";
}

function drawAnalysis() {
 if (S.screen !== "analysis") return;
 const waiting = !S.history && S.live.state === "live" && !S.stints.length;
 $("wait").hidden = !waiting; $("wait").textContent = waiting ? t("analysis.waiting") : "";
 readK(); buildDock(); LA.dirty = true;
 requestAnimationFrame(refreshThumbs);
}

function redraw() { if (S.screen === "analysis") drawAnalysis(); if (S.screen === "setup") renderSetup(); if (S.screen === "driver") loadDriver(); }

// ---------- single poll on /api/state (ui stamp, watcher, live session) ----------
let importStamp = null, uiStamp = null, down = false, refreshing = false;
async function applyState(st) {
 S.state = st;
 if (down || (uiStamp != null && st.ui && st.ui !== uiStamp)) { location.reload(); return; }
 if (st.ui) uiStamp = st.ui;
 down = false;
 const w = st.watcher || {};
 showError(w.last_error);
 if (st.live) {
  const o = st.live, was = S.live;
  S.live = o;
  if (!S.history && o.state === "live") {
   if (o.session && (!S.session || S.session.id !== o.session)) { await openSession(o.session, false); if (S.screen === "home") show("analysis"); }
   else if (!o.session && S.screen === "home") show("analysis");
  }
  if (was.state !== o.state) { renderNav(); drawAnalysis(); }
 }
 const stamp = `${w.imported}|${w.last_import}`;
 const imported = importStamp !== null && stamp !== importStamp;
 importStamp = stamp;
 if (imported && S.session) {
  if (!S.history && S.live.session && S.live.session !== S.session.id) await openSession(S.live.session, false);
  else await loadStints(true);
 }
}

async function refresh() {
 if (refreshing) return;
 refreshing = true;
 try { await applyState(await api("/api/state")); }
 catch (e) { down = true; }
 finally { refreshing = false; }
}

// ---------- home ----------
async function loadHome() {
 const box = $("home");
 let sessions = [], fact = "";
 try { sessions = (await api("/api/sessions")).sessions; } catch (e) { showError(e.message); }
 try { fact = (await api("/api/welcome")).fact || ""; } catch (e) {}
 const live = sessions.filter(s => s.laps > 0).sort((a, b) => when(b.recorded_at).k.localeCompare(when(a.recorded_at).k));
 const hero = el("div", {class: "hero"}), recent = el("div", {class: "recent"});
 hero.append(el("div", {class: "hello"}, t("home.hello")));
 if (fact) hero.append(el("div", {class: "fact"}, fact));
 const stat = el("div", {class: "stat"}), w = S.state && S.state.watcher;
 stat.innerHTML = `<span>${S.live.state === "live" ? t("nav.live", {session: S.live.session_name || S.live.track || ""}) : t("nav.off")}</span><span>${t("home.sessions", {n: `<b>${sessions.length}</b>`})}</span>` +
  (w && w.last_import ? `<span>${t("home.lastimport", {when: `<b>${new Date(w.last_import * 1000).toLocaleString(LANG)}</b>`})}</span>` : "");
 hero.append(stat);
 if (!live.length) hero.append(el("div", {class: "fact"}, t("home.start")));
 else {
  const last = live[0], lw = when(last.recorded_at);
  const c = el("div", {class: "cont"});
  c.innerHTML = `<div class="k">${t("home.continue")}</div><div><div class="trk"></div><div class="car"><span class="nm"></span><span class="cls"></span></div></div><div class="lt">${fmtLap(last.best)}<small>${t("home.best", {date: lw.d})}</small></div>`;
  c.querySelector(".trk").textContent = shortTrack(last.track); c.querySelector(".nm").textContent = last.car_name; c.querySelector(".cls").textContent = className(last.car_class);
  const acts = el("div", {class: "acts"}), a1 = el("button", {type: "button", class: "btnp"}, t("home.open")), a2 = el("button", {type: "button", class: "btnt"}, t("home.next"));
  a1.onclick = async () => { await openSession(last.id, true); show("analysis"); }; a2.onclick = async () => { await openSession(last.id, true); show("setup"); };
  acts.append(a1, a2); c.append(acts); hero.append(c);
  const ws = new Date(lw.date); ws.setDate(ws.getDate() - 6);
  const wk = live.filter(s => when(s.recorded_at).date >= ws), week = el("div", {class: "week"});
  for (const [v, k] of [[wk.length, "home.wk_sessions"], [wk.reduce((a, s) => a + s.laps, 0), "home.wk_laps"], [new Set(wk.map(s => s.track)).size, "home.wk_tracks"]]) { const d = el("div", {class: "wk"}); d.append(el("div", {class: "v"}, String(v)), el("div", {class: "k"}, t(k))); week.append(d); }
  hero.append(week);
 }
 const rh = el("div", {class: "recenth"}), all = el("button", {type: "button", class: "linkbtn"}, t("home.all")); all.onclick = () => show("history");
 rh.append(el("span", {}, t("home.recent")), all); recent.append(rh);
 const seen = new Set();
 for (const s of live) { const k = s.car_name + "|" + s.track; if (seen.has(k)) continue; seen.add(k); if (seen.size > 6) break;
  const r = el("button", {type: "button", class: "rr", "data-tip": t("tip.openanalysis")}), w2 = when(s.recorded_at);
  r.append(el("span", {class: "t"}, shortTrack(s.track)), el("span", {class: "b"}, fmtLap(s.best)), el("span", {class: "s"}, s.car_name), el("span", {class: "d"}, w2.d));
  r.onclick = async () => { await openSession(s.id, true); show("analysis"); }; recent.append(r); }
 box.replaceChildren(hero, recent);
}

// ---------- history ----------
async function loadHistory() {
 let sessions = [];
 try { sessions = (await api("/api/sessions")).sessions; } catch (e) { showError(e.message); }
 const m = new Map();
 sessions.filter(s => s.laps > 0).forEach(s => { const k = s.car_name + "|" + s.track; if (!m.has(k)) m.set(k, {k, car: s.car_name, model: s.model ? carName(s.model) : "", track: s.track, cls: className(s.car_class), sess: []}); m.get(k).sess.push(s); });
 S.groups = [...m.values()].map(g => { g.sess.sort((a, b) => when(b.recorded_at).k.localeCompare(when(a.recorded_at).k)); g.best = Math.min(...g.sess.map(s => s.best).filter(v => v != null)); g.laps = g.sess.reduce((a, s) => a + s.laps, 0); g.last = when(g.sess[0].recorded_at); return g; });
 if (!S.hopen.size && S.groups.length) S.hopen.add([...S.groups].sort((a, b) => b.last.k.localeCompare(a.last.k))[0].k);
 renderHistory();
}

function spark(g) {
 const pts = [...g.sess].reverse().map(s => s.best).filter(v => v != null);
 if (pts.length < 2) return el("span", {class: "sub"}, t("history.one"));
 let run = Infinity; const pb = pts.map(v => (run = Math.min(run, v))), lo = Math.min(...pb), hi = Math.max(...pb), W = 150, H = 30, X = i => i / (pb.length - 1) * (W - 8) + 4, Y = v => hi === lo ? H / 2 : 4 + (v - lo) / (hi - lo) * (H - 8);
 const s = sv("svg", {class: "spark", viewBox: `0 0 ${W} ${H}`, "aria-label": t("history.trend")});
 s.append(sv("path", {d: pb.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(""), fill: "none", stroke: css("--pri"), "stroke-width": 1.8, "stroke-linejoin": "round"}), sv("circle", {cx: X(pb.length - 1), cy: Y(pb.at(-1)), r: 3, fill: css("--pri")}));
 return s;
}

function renderHistory() {
 const box = $("history"), groups = S.groups || [];
 const bar = el("div", {class: "bar"}), search = el("label", {class: "search"}), inp = el("input", {type: "search", placeholder: t("history.search"), "aria-label": t("history.search")});
 inp.value = S.hq; inp.oninput = () => { S.hq = inp.value; renderList(); };
 search.innerHTML = '<svg class="ico" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>'; search.append(inp);
 const classes = ["all", ...[...new Set(groups.map(g => g.cls))].sort()];
 const count = el("span", {class: "count"});
 bar.append(search, el("span", {class: "kq"}, t("history.class")), pills(null, classes, classes.map(c => c === "all" ? t("history.all") : c), S.hcls, v => { S.hcls = v; renderList(); }),
  el("span", {class: "kq"}, t("history.sort")), pills(null, ["recent", "track", "laps"], [t("history.recent"), t("history.track"), t("history.mostlaps")], S.hsort, v => { S.hsort = v; renderList(); }), el("span", {class: "grow"}), count);
 const list = el("div", {class: "hl"});
 box.replaceChildren(bar, list);
 function renderList() {
  const q = S.hq.trim().toLowerCase();
  let gs = groups.filter(g => (S.hcls === "all" || g.cls === S.hcls) && (!q || (g.car + " " + g.model + " " + g.track + " " + shortTrack(g.track)).toLowerCase().includes(q)));
  gs.sort(S.hsort === "track" ? (a, b) => shortTrack(a.track).localeCompare(shortTrack(b.track)) : S.hsort === "laps" ? (a, b) => b.laps - a.laps : (a, b) => b.last.k.localeCompare(a.last.k));
  count.textContent = t("history.count", {n: gs.length, s: gs.reduce((a, g) => a + g.sess.length, 0)});
  if (!gs.length) { list.replaceChildren(el("div", {class: "grp empty"}, groups.length ? t("history.nomatch") : t("history.none"))); return; }
  list.replaceChildren(...gs.map(g => {
   const open = S.hopen.has(g.k), d = el("div", {class: "grp" + (open ? " open" : "")}), h = el("button", {type: "button", class: "gh", "aria-expanded": open});
   const tcell = el("div"); tcell.append(el("div", {class: "trk"}, shortTrack(g.track)), el("div", {class: "sub"}, g.track));
   const car = el("div", {class: "carc"}), top = el("div", {class: "car"});
   top.append(el("span", {class: "nm", title: g.model || g.car}, g.model || g.car), el("span", {class: "cls"}, g.cls)); car.append(top);
   if (g.model) car.append(el("div", {class: "sub", title: g.car}, g.car));
   const n1 = el("div", {class: "num"}, String(g.sess.length)); n1.append(el("small", {}, t(g.sess.length === 1 ? "history.session" : "history.sessions")));
   const n2 = el("div", {class: "num"}); n2.append(el("span", {class: "best"}, fmtLap(g.best)), el("small", {}, t(g.laps === 1 ? "stint.lap" : "stint.laps", {n: g.laps})));
   const sp = el("div"); sp.append(spark(g));
   const dt = el("div", {class: "date"}, g.last.d); dt.append(el("div", {class: "sub"}, g.last.t));
   h.append(tcell, car, n1, n2, sp, dt); h.insertAdjacentHTML("beforeend", '<svg class="chev" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>');
   h.onclick = () => { if (S.hopen.has(g.k)) S.hopen.delete(g.k); else S.hopen.add(g.k); renderList(); };
   d.append(h);
   if (open) {
    const ss = el("div", {class: "hsess"}), hd = el("div", {class: "shead"});
    for (const k of ["history.h_session", "history.h_stints", "history.h_laps", "history.h_best", "history.h_tobest"]) hd.append(el("span", {}, t(k)));
    hd.append(el("span")); ss.append(hd);
    for (const s of g.sess) {
     const w = when(s.recorded_at), pb = s.best === g.best, r = el("button", {type: "button", class: "sr", "data-tip": t("tip.openanalysis")}), dd = el("span", {}, w.d + " ");
     dd.append(el("span", {class: "k"}, w.t));
     r.append(dd, el("span", {}, String(s.stints)), el("span", {}, String(s.laps)), el("span", {class: pb ? "pb" : ""}, fmtLap(s.best)), el("span", {class: "d"}, pb ? t("history.isbest") : `+${fx(s.best - g.best, 3)} s`));
     r.insertAdjacentHTML("beforeend", '<svg class="go" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>');
     r.onclick = async () => { await openSession(s.id, true); show("analysis"); };
     ss.append(r);
    }
    d.append(ss);
   }
   return d;
  }));
 }
 renderList();
 requestAnimationFrame(refreshThumbs);
}

// ---------- driver ----------
async function loadDriver() {
 const box = $("driver");
 let d; try { d = await api("/api/driver"); } catch (e) { showError(e.message); return; }
 if (!d.classes.length) { box.replaceChildren(el("div", {class: "sec empty"}, t("stints.none"))); return; }
 const COLS = ["--pri", "--ter", "--t1"];
 const say = (v, neg, pos) => v == null ? t("driver.nofast") : Math.abs(v) < 0.005 ? t("driver.asusual") : `${t(v < 0 ? neg : pos)} ${fx(Math.abs(v), 2)}°`;
 box.replaceChildren(...d.classes.map(c => {
  const sec = el("div", {class: "sec"}), h = el("h2", {}, className(c.car_class)); h.append(el("small", {}, t("stint.laps", {n: c.laps}))); sec.append(h);
  if (!c.cars.length) { sec.append(el("div", {class: "muted"}, t("driver.none"))); return sec; }
  const col = i => css(COLS[i % 3]);
  const step = c.step || 0.25, M = c.span || 2 * step, OUT = 26;
  const W = 600, H = 500, cx = W / 2, cy = H / 2 + 6, R = 180, AX = [
   {k: "front", a: -90, pos: "feel.lazier", neg: "feel.pointier", name: "driver.front"},
   {k: "rear", a: -30, pos: "feel.safer", neg: "feel.looser", name: "driver.rear"},
   {k: "fast", a: -150, pos: "feel.planted", neg: "feel.looser", name: "driver.fast"}];
  const rad = v => { const a = Math.abs(v); return Math.sign(v) * (a <= M ? a / M * R : R + OUT * (1 - M / a)); };
  const pt = (ax, v, r = rad(v)) => { const th = ax.a * Math.PI / 180; return [cx + r * Math.cos(th), cy + r * Math.sin(th)]; };
  const s = sv("svg", {class: "plot", viewBox: `0 0 ${W} ${H}`, role: "img"});
  const ring = r => Array.from({length: 6}, (_, i) => { const th = (i * 60 - 90) * Math.PI / 180; return `${cx + r * Math.cos(th)},${cy + r * Math.sin(th)}`; }).join(" ");
  s.append(sv("polygon", {points: ring(R + OUT + 6), fill: css("--l2"), stroke: "none"}));
  for (let k = step; k <= M + 1e-9; k += step) s.append(sv("polygon", {points: ring(k / M * R), fill: "none", stroke: css("--t3"), "stroke-opacity": k + step > M + 1e-9 ? .6 : .25, "stroke-dasharray": k + step > M + 1e-9 ? "4 4" : null}));
  for (const ax of AX) {
   const [x1, y1] = pt(ax, 0, R + OUT), [x2, y2] = pt(ax, 0, -R - OUT); s.append(sv("line", {x1, y1, x2, y2, stroke: css("--t3"), "stroke-opacity": .6}));
   for (const [v, key] of [[M, ax.pos], [-M, ax.neg]]) {
    const [x, y] = pt(ax, 0, Math.sign(v) * (R + OUT + 22)), anchor = Math.abs(x - cx) < 5 ? "middle" : x < cx ? "end" : "start", up = y < cy;
    const tx = sv("text", {x, y: y + (up ? -4 : 12), "text-anchor": anchor, fill: css("--t1"), "font-size": 14, "font-weight": 500}, t(key));
    const sub = sv("text", {x, y: y + (up ? 13 : 29), "text-anchor": anchor, fill: css("--t3"), "font-size": 12}, t(ax.name));
    s.append(tx, sub);
   }
  }
  s.append(sv("circle", {cx, cy, r: 4, fill: css("--t3")}));
  c.cars.forEach((x, i) => {
   const ps = AX.map(ax => pt(ax, x[ax.k] ?? 0)), g = sv("g", {"data-tip": [carName(x.car_key), ...AX.map(ax => `${t(ax.name)}: ${say(x[ax.k], ax.neg, ax.pos)}`)].join(" · ")});
   g.append(sv("polygon", {points: ps.map(p => p.join(",")).join(" "), fill: col(i), "fill-opacity": .14, stroke: col(i), "stroke-width": 2.5, "stroke-linejoin": "round"}));
   ps.forEach(([px, py], j) => { const v = x[AX[j].k]; if (v == null) return;
    const far = Math.abs(v) > M; g.append(sv("circle", {cx: px, cy: py, r: 6, fill: far ? css("--l1") : col(i), stroke: far ? col(i) : css("--l1"), "stroke-width": far ? 3 : 2}));
    if (far) { const right = px >= cx; g.append(sv("text", {x: px + (right ? 12 : -12), y: py + 4, "text-anchor": right ? "start" : "end", fill: col(i), "font-size": 12.5, "font-weight": 600}, `${fx(Math.abs(v), 2)}°`)); } });
   s.append(g);
  });
  const mapc = el("div", {class: "mapc"}); mapc.append(s);
  const lines = el("div", {class: "cars"});
  c.cars.forEach((x, i) => { const l = el("div", {class: "carl", "data-tip": `${x.layout} · ${fmtStamp(x.created_at)}`}); l.innerHTML = `<span class="d" style="background:${col(i)}"></span>`; l.append(el("b", {}, carName(x.car_key)), el("span", {}, x.line));
   for (const ax of AX) l.append(el("span", {class: "ax"}, `${t(ax.name)}: ${say(x[ax.k], ax.neg, ax.pos)}`)); lines.append(l); });
  const wrap = el("div", {class: "drv"}); wrap.append(mapc, lines); sec.append(wrap); return sec;
 }));
}

// ---------- settings ----------
const FOLDER = {install: "settings.f_install", telemetry: "settings.f_telemetry", settings: "settings.f_settings", database: "settings.f_database"};
let confirmForget = null, dataStatus = "";
async function loadSettings() { try { S.settings = await api("/api/settings"); Object.assign(UNIT, S.settings.units); setShortTracks(S.settings.short_tracks); } catch (e) { showError(e.message); return; } renderSettings(); }
async function putSettings(body) { try { S.settings = await api("/api/settings", body); Object.assign(UNIT, S.settings.units); } catch (e) { showError(e.message); return; } renderSettings(); if (body.units) { S.proposal = null; if (S.screen === "setup") await loadProposal(); } }
function row(label, mid, right) { const r = el("div", {class: "row"}); r.append(el("span", {class: "k"}, label), mid || el("span"), right || el("span")); return r; }
function sec(title, ...rows) { const s = el("div", {class: "sec"}); s.append(el("h2", {}, title), ...rows); return s; }

function renderSettings() {
 const st = S.settings, box = $("settings"); if (!st) return;
 const ch = st.unit_choices, langs = Object.keys(LANGS);
 const app = sec(t("settings.appearance"),
  row(t("settings.theme"), null, pills(null, ["system", "dark", "light"], [t("settings.system"), t("settings.dark"), t("settings.light")], themePref(), v => setThemePref(v))),
  row(t("settings.colour"), null, pills(null, SCHEMES, SCHEMES.map(s => t("scheme." + s)), schemePref(), v => setSchemePref(v))),
  row(t("settings.language"), null, pills(null, langs, langs.map(l => LANGS[l]), LANG, async v => { try { localStorage.setItem("autosetup.lang", v); } catch (e) {} await loadStrings(); renderNav(); show(S.screen); })));
 const units = sec(t("settings.units"),
  row(t("settings.speed"), null, pills(null, ch.speed, ch.speed, st.units.speed, v => putSettings({units: {speed: v}}))),
  row(t("settings.temp"), null, pills(null, ch.temp, ch.temp, st.units.temp, v => putSettings({units: {temp: v}}))),
  row(t("settings.pressure"), null, pills(null, ch.pressure, ch.pressure, st.units.pressure, v => putSettings({units: {pressure: v}}))));
 const tp = st.templates || {}, target = S.proposal && S.proposal.target;
 const tpl = (key, label, preview) => { const f = el("label", {class: "field"}), i = el("input", {type: "text", value: tp[key] || tp["default_" + key], spellcheck: "false", "aria-label": label});
  i.onchange = async () => { await putSettings({templates: {[key]: i.value || tp["default_" + key]}}); S.proposal = null; if (S.screen === "setup") await loadProposal(); renderSettings(); }; f.append(i, el("span", {class: "prev"}, preview || "")); return row(label, f); };
 const files = sec(t("settings.files"), tpl("name", t("settings.name"), target && target.file), tpl("notes", t("settings.desc"), target && target.notes));
 const data = sec(t("settings.data"));
 for (const f of st.folders) { const o = el("button", {type: "button", class: "tb"}, t("settings.open")); o.disabled = !f.path; o.onclick = async () => { try { await api("/api/open-folder", {which: f.which}); } catch (e) { showError(e.message); } }; data.append(row(t(FOLDER[f.which] || f.which), el("span", {class: "path"}, f.path || "–"), o)); }
 const imp = el("button", {type: "button", class: "tb"}, t("settings.import")); imp.onclick = async () => { imp.disabled = true; try { const r = await api("/api/import", {}); dataStatus = t("settings.imported", {n: r.imported}); } catch (e) { showError(e.message); } renderSettings(); };
 const rec = el("button", {type: "button", class: "tb"}, st.recomputing ? t("settings.recomputing") : t("settings.recompute")); rec.disabled = st.recomputing;
 rec.onclick = async () => { try { await api("/api/recompute", {}); } catch (e) { showError(e.message); return; } await loadSettings(); pollRecompute(); };
 const acts = el("div", {class: "acts"}); acts.append(imp, rec);
 data.append(row(t("settings.recordings"), el("span", {class: "path muted"}, st.recomputing ? t("settings.recomputing") : dataStatus || t("settings.uptodate")), acts));
 data.append(row(t("settings.phone"), el("span", {class: "path muted"}, st.phone_access && st.phone_url ? st.phone_url : t("settings.phone_sub")),
  pills(null, [false, true], [t("kerbs.off"), t("kerbs.on")], !!st.phone_access, v => putSettings({phone_access: v}))));
 const prof = sec(t("settings.profile")), fps = st.fingerprints || [];
 if (!fps.length) prof.append(el("div", {class: "muted"}, t("driver.none")));
 for (const f of fps) {
  const info = el("span", {class: "path muted"}, `${className(f.car_class)} · ${f.layout} · ${fmtStamp(f.created_at)}`);
  if (confirmForget === f.car_key) {
   const a2 = el("div", {class: "acts"}), no = el("button", {type: "button", class: "tb"}, t("settings.cancel")), yes = el("button", {type: "button", class: "tb warn"}, t("settings.confirmforget"));
   no.onclick = () => { confirmForget = null; renderSettings(); }; yes.onclick = async () => { confirmForget = null; try { await api("/api/forget", {car_key: f.car_key}); } catch (e) { showError(e.message); } S.proposal = null; await loadSettings(); };
   a2.append(no, yes); prof.append(row(carName(f.car_key), info, a2));
  } else { const b = el("button", {type: "button", class: "tb warn"}, t("settings.forget")); b.onclick = () => { confirmForget = f.car_key; renderSettings(); }; prof.append(row(carName(f.car_key), info, b)); }
 }
 box.replaceChildren(app, units, files, data, prof);
 requestAnimationFrame(refreshThumbs);
}

async function pollRecompute() { await loadSettings(); if (S.settings && S.settings.recomputing) setTimeout(pollRecompute, 1000); }

// ---------- about ----------
async function openAbout() {
 $("about").hidden = false; $("about-close").focus();
 const b = $("about-body"); b.replaceChildren();
 try { const a = await api("/api/about"); for (const [k, v] of [["about.version", a.build ? `${a.version} (${a.build})` : a.version], ["about.metrics", a.metrics_version], ["about.database", a.database], ["about.recordings", a.recordings], ["about.laps", a.laps]]) b.append(el("dt", {}, t(k)), el("dd", {}, String(v ?? "–"))); }
 catch (e) { b.append(el("dd", {}, e.message)); }
}
$("about-close").onclick = () => { $("about").hidden = true; };
$("about").addEventListener("click", e => { if (e.target === $("about")) $("about").hidden = true; });
document.addEventListener("keydown", e => { if (e.key === "F5" || (e.ctrlKey && e.key.toLowerCase() === "r")) { e.preventDefault(); location.reload(); } });

// ---------- start ----------
initMap();
initTraces();
initAnalysisLoop();
(async () => {
 await loadStrings();
 setThemePref(themePref()); setSchemePref(schemePref());
 try { const st = await api("/api/state"); S.state = st; uiStamp = st.ui; const w = st.watcher || {}; showError(w.last_error); importStamp = `${w.imported}|${w.last_import}`; if (st.live) S.live = st.live; } catch (e) { showError(e.message); }
 try { S.settings = await api("/api/settings"); Object.assign(UNIT, S.settings.units); setShortTracks(S.settings.short_tracks); } catch (e) {}
 renderNav();
 if (S.live.state === "live" && S.live.session) { await openSession(S.live.session, false); show("analysis"); }
 else if (S.live.state === "live") show("analysis");
 else show("home");
 setInterval(refresh, 1000);
})();
