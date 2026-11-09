export const $ = id => document.getElementById(id);
export const SVGNS = "http://www.w3.org/2000/svg";

export function el(tag, attrs = {}, text) {
 const e = document.createElement(tag);
 for (const [k, v] of Object.entries(attrs)) { if (k === "class") e.className = v; else if (k.startsWith("on")) e[k] = v; else e.setAttribute(k, v); }
 if (text != null) e.textContent = text;
 return e;
}

export function sv(tag, attrs = {}, text) {
 const e = document.createElementNS(SVGNS, tag);
 for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
 if (text != null) e.textContent = text;
 return e;
}

export function svgBox(box, width, height) {
 const s = sv("svg", {viewBox: `0 0 ${width} ${height}`, role: "img"});
 box.append(s);
 return s;
}

export const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

export async function api(url, body) {
 const opt = body === undefined ? {cache: "no-store"} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
 const r = await fetch(url, opt);
 const data = await r.json().catch(() => ({}));
 if (!r.ok) throw new Error(data.error || r.statusText);
 return data;
}

// ---------- strings: every visible text comes from web/i18n/<lang>.json ----------
export let STR = {}, LANGS = {en: "English"}, LANG = "en";
export const t = (k, v) => { let s = STR[k] ?? k; if (v) for (const [a, b] of Object.entries(v)) s = s.split("{" + a + "}").join(b ?? ""); return s; };

export async function loadStrings() {
 try { LANG = localStorage.getItem("autosetup.lang") || "en"; } catch (e) {}
 try { LANGS = await (await fetch("/i18n/languages.json", {cache: "no-store"})).json(); } catch (e) {}
 const get = async l => (await fetch(`/i18n/${l}.json`, {cache: "no-store"})).json();
 try { STR = await get("en"); } catch (e) { STR = {}; }
 if (LANG !== "en") { try { STR = {...STR, ...(await get(LANG))}; } catch (e) { LANG = "en"; } }
 document.documentElement.lang = LANG;
 document.title = t("app.title");
 for (const e of document.querySelectorAll("[data-t]")) e.textContent = t(e.dataset.t);
 for (const e of document.querySelectorAll("[data-tt]")) { e.setAttribute("aria-label", t(e.dataset.tt)); e.setAttribute("title", t(e.dataset.tt)); }
}

// ---------- units and numbers ----------
export const UNIT = {speed: "km/h", temp: "°C", pressure: "kPa"};
export const cv = (q, v) => v == null || !isFinite(v) ? v : q === "speed" && UNIT.speed === "mph" ? v * 0.621371 : v;
export const fmtLap = s => s == null || !isFinite(s) ? "–" : `${Math.floor(s / 60)}:${(s % 60).toFixed(3).padStart(6, "0")}`;
export const fx = (v, d = 2) => v == null || !isFinite(v) ? "–" : Number(v).toFixed(d);
export const signed = (v, d = 0) => v == null || !isFinite(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d);
export const when = s => { const m = String(s || "").match(/(\d{4})-(\d\d)-(\d\d)T(\d\d)_(\d\d)/); return m ? {d: t("fmt.date", {d: +m[3], m: +m[2], y: m[1]}), t: `${m[4]}:${m[5]}`, k: m.slice(1).join(""), date: new Date(+m[1], +m[2] - 1, +m[3])} : {d: String(s || ""), t: "", k: String(s || ""), date: new Date(0)}; };
export const fmtStamp = s => { const m = String(s).match(/(\d{4})-(\d\d)-(\d\d)/); return m ? t("fmt.date", {d: +m[3], m: +m[2], y: m[1]}) : String(s); };
let SHORT = {};  // track → short name, from the server (templates.SHORT_TRACKS): the same names the setup files use
export const setShortTracks = s => { SHORT = s || SHORT; };
export const shortTrack = n => SHORT[n] || n || "";
const CLASS = {LMP2_ELMS: "LMP2 ELMS"};  // WEC's LMP2 and ELMS's are separate classes: keep their names apart
export const className = c => CLASS[c] || c || "";
export const carName = k => String(k).replace(/_/g, " ");
export const showError = msg => { $("err").textContent = msg || ""; $("err").title = msg || ""; };

// ---------- state ----------
export const S = {screen: "home", prev: null, state: null, live: {state: "off"}, history: false, session: null, stints: [], open: new Set(),
 a: null, b: null, lapA: null, lapB: null, delta: null, map: null, mapCtx: null, line: "pedals",
 t: 0, play: false, follow: false, proposal: null, proposalBusy: false, written: new Set(), hist: {kerbs: "off"}, hists: {},
 settings: null, feelTarget: null, hq: "", hcls: "all", hsort: "recent", hopen: new Set()};

export const LA = {win: 200, hov: null, dirty: true, K: {}, G: null, wh: [1, 1], dw: [1, 1], fitS: 1, drag: null, last: 0};

// ---------- pills: one shape, the chosen option is a pill that morphs between options ----------
export function placeThumb(g, animate) {
 let th = g.querySelector(".thumb"); if (!th) { th = el("span", {class: "thumb"}); g.prepend(th); }
 const b = g.querySelector('button[aria-pressed="true"]');
 if (!b || !g.offsetWidth) { th.style.width = "0"; return; }
 if (animate) { th.classList.add("go"); setTimeout(() => th.classList.remove("go"), 200); }
 th.style.left = (b.offsetLeft - 2) + "px"; th.style.width = (b.offsetWidth + 4) + "px";
}

export function pills(box, values, labels, current, onPick, big) {
 const g = typeof box === "string" ? $(box) : box || el("div", {class: "seg", role: "group"});
 g.className = "seg" + (big ? " big" : "");
 g.replaceChildren(...values.map((v, i) => {
  const b = el("button", {type: "button", "aria-pressed": v === current}, labels[i]);
  b.onclick = () => { if (b.getAttribute("aria-pressed") === "true") return;
   g.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", x === b)); placeThumb(g, true); onPick(v); };
  return b;
 }));
 requestAnimationFrame(() => placeThumb(g, false));
 return g;
}

export const refreshThumbs = () => document.querySelectorAll(".seg").forEach(g => placeThumb(g, false));
window.addEventListener("resize", refreshThumbs);

// ---------- stint & lap helpers ----------
export const stintNo = st => S.stints.indexOf(st) + 1;
export const stintById = id => S.stints.find(s => s.id === id) || null;
export const racingLaps = st => st.laps.filter(l => l.complete && !l.in_pits);
export const lapKey = (stint, seq) => `${stint}:${seq}`;
export const parseKey = k => { const [a, b] = String(k).split(":").map(Number); return {stint: a, seq: b}; };
export function bestLap(st) { const laps = racingLaps(st), clean = laps.filter(l => l.clean), pool = clean.length ? clean : laps; return pool.length ? pool.reduce((a, l) => (l.time < a.time ? l : a)) : null; }
export const baseStint = () => [...S.stints].reverse().find(s => s.laps.some(l => !l.in_pits)) || S.stints[S.stints.length - 1] || null;
export const keyOk = k => k && S.stints.some(s => racingLaps(s).some(l => lapKey(s.id, l.seq) === k));
export function bestOverall() { let best = null; for (const s of S.stints) { const l = bestLap(s); if (l && l.clean && (!best || l.time < best.l.time)) best = {s, l}; } return best; }

export const compare = () => !!(S.lapA && S.lapB);
export function at(L, key, d) {
 if (!L) return null;
 const tr = L.trace, n = tr.distance.length, f = d / tr.step_m, i = Math.floor(f);
 if (i < 0) return tr[key][0]; if (i >= n - 1) return tr[key][n - 1];
 const a = tr[key][i], b = tr[key][i + 1];
 return a == null || b == null ? (a ?? b) : a + (b - a) * (f - i);
}
export const timeAt = (L, d) => at(L, "time", d);
export function distAt(L, s) {
 if (!L) return 0;
 const tr = L.trace, ts = tr.time; let lo = 0, hi = ts.length - 1;
 while (hi > 0 && ts[hi] == null) hi--;
 if (s <= (ts[0] ?? 0)) return tr.distance[0]; if (s >= ts[hi]) return tr.distance[hi];
 while (hi - lo > 1) { const m = (lo + hi) >> 1; if (ts[m] != null && ts[m] <= s) lo = m; else hi = m; }
 return tr.distance[lo] + (s - ts[lo]) / ((ts[hi] - ts[lo]) || 1) * (tr.distance[hi] - tr.distance[lo]);
}
export const lapT = () => S.lapA ? S.lapA.time : 0;
export const dA = () => distAt(S.lapA, S.t);
export const dB = () => distAt(S.lapB, S.t);  // lap B at the same time in the lap as B's dot on the map
export const lapLabel = L => L ? t("lap.n", {n: L.lap}) : "";
export const idxNow = () => LA.G ? clamp(Math.round(dA() / LA.G.STEP), 0, LA.G.N - 1) : 0;
export function readK() { for (const k of ["l1", "l2", "l3", "bd", "t1", "t2", "t3", "green", "red", "pri", "ter", "lapA", "lapB"]) LA.K[k] = css("--" + k); LA.dirty = true; }
export function mix(c1, c2, f) { const h = c => c.length === 4 ? [1, 2, 3].map(i => parseInt(c[i] + c[i], 16)) : [1, 3, 5].map(i => parseInt(c.slice(i, i + 2), 16)), a = h(c1), b = h(c2); return "rgb(" + a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(",") + ")"; }
export const smoothW = (arr, w) => arr.map((_, i) => { let s = 0, n = 0; for (let k = -w; k <= w; k++) { const j = i + k; if (j >= 0 && j < arr.length) { s += arr[j]; n++; } } return s / n; });
