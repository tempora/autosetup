import {$, clamp, signed, cv, t, S, LA, idxNow, mix, smoothW} from "./state.js";

// ---------- geometry for the loaded laps: driven lines, road centre and real width, zones ----------
export function buildGeo() {
 LA.G = null; const A = S.lapA, B = S.lapB; if (!A) return;
 const ta = A.trace, tb = B && B.trace, STEP = ta.step_m, N = tb ? Math.min(ta.distance.length, tb.distance.length) : ta.distance.length;
 if (N < 8) return;
 // gaps filled by straight lines; null when the channel is missing altogether
 const fill = arr => { if (!arr) return null; const out = arr.slice(0, N); let last = -1;
  for (let i = 0; i < N; i++) if (out[i] != null) { if (last < 0) for (let j = 0; j < i; j++) out[j] = out[i]; else for (let j = last + 1; j < i; j++) out[j] = out[last] + (out[i] - out[last]) * (j - last) / (i - last); last = i; }
  if (last < 0) return null; for (let j = last + 1; j < N; j++) out[j] = out[last]; return out; };
 // GPS at 5 m steps is slightly jittery: smooth the driven lines (binomial 1-4-6-4-1, two passes) so lines and cars run smoothly
 const bin = a => { let s = a; for (let p = 0; p < 2; p++) { const src = s; s = src.map((_, i) => { let v = 0, w = 0; for (const [o, c] of [[-2, 1], [-1, 4], [0, 6], [1, 4], [2, 1]]) { const j = i + o; if (j >= 0 && j < N) { v += src[j] * c; w += c; } } return v / w; }); } return s; };
 const mp = S.map && S.map.points;
 const fromMap = (d, k) => { let lo = 0, hi = mp.length - 1; if (d <= mp[0][0]) return mp[0][k]; if (d >= mp[hi][0]) return mp[hi][k];
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (mp[m][0] <= d) lo = m; else hi = m; } return mp[lo][k] + (mp[hi][k] - mp[lo][k]) * (d - mp[lo][0]) / ((mp[hi][0] - mp[lo][0]) || 1); };
 const line = tr => { let x = fill(tr.x), y = fill(tr.y);
  if (!x || !y) { if (!mp || !mp.length) return null; x = []; y = []; for (let i = 0; i < N; i++) { x.push(fromMap(i * STEP, 1)); y.push(fromMap(i * STEP, 2)); } }
  return {x: bin(x), y: bin(y)}; };
 const la = line(ta), lb = tb ? line(tb) : null; if (!la) return;
 const Mn = Array.from({length: N}, (_, i) => lb ? [(la.x[i] + lb.x[i]) / 2, (la.y[i] + lb.y[i]) / 2] : [la.x[i], la.y[i]]);
 const nL = Mn.map((_, i) => { const a = Mn[Math.max(0, i - 3)], b = Mn[Math.min(N - 1, i + 3)], dx = b[0] - a[0], dy = b[1] - a[1], l = Math.hypot(dx, dy) || 1; return [-dy / l, dx / l]; });
 // road centre from Path Lateral: positive is right of the centre, and nL points left, so centre = line + nL · lateral
 const pa = fill(ta.plat), pb = tb ? fill(tb.plat) : null;
 const centre = (l, p) => Array.from({length: N}, (_, i) => [l.x[i] + nL[i][0] * p[i], l.y[i] + nL[i][1] * p[i]]);
 let Cx, Cy;
 if (pa) { const ca = centre(la, pa), cb = lb && pb ? centre(lb, pb) : ca; Cx = smoothW(ca.map((p, i) => (p[0] + cb[i][0]) / 2), 3); Cy = smoothW(ca.map((p, i) => (p[1] + cb[i][1]) / 2), 3); }
 else { Cx = Mn.map(p => p[0]); Cy = Mn.map(p => p[1]); }
 // half width from Track Edge, both laps averaged; 6 m where the channel is missing
 const ea = fill(ta.edge), eb = tb ? fill(tb.edge) : null;
 const HW = smoothW(Array.from({length: N}, (_, i) => !ea ? 6 : clamp(eb ? (Math.abs(ea[i]) + Math.abs(eb[i])) / 2 : Math.abs(ea[i]), 5, 14)), 6);
 const EL = Cx.map((x, i) => [x + nL[i][0] * HW[i], Cy[i] + nL[i][1] * HW[i]]), ER = Cx.map((x, i) => [x - nL[i][0] * HW[i], Cy[i] - nL[i][1] * HW[i]]);
 const xs = [...EL, ...ER].map(p => p[0]), ys = [...EL, ...ER].map(p => p[1]);
 const BX = {x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys)};
 // pedals, speed and gear for the deck; gear 0 is a shift in progress, so the last gear carries through it
 const lap = (L, l) => { const tr = L.trace, g = []; for (let i = 0; i < N; i++) g.push(tr.gear[i] > 0 ? tr.gear[i] : i ? g[i - 1] : 1);
  return {L, tr, x: l.x, y: l.y, t: tr.time.slice(0, N), v: tr.speed.slice(0, N).map(v => cv("speed", v ?? 0)), th: tr.throttle.slice(0, N).map(v => v || 0), br: tr.brake.slice(0, N).map(v => v || 0), g}; };
 const GA = lap(A, la), GB = B ? lap(B, lb) : null;
 for (const Q of [GA, GB]) if (Q) { const ts = (r) => Array.from({length: N}, (_, i) => { const a = Math.max(0, i - r), b = Math.min(N - 1, i + r), dx = Q.x[b] - Q.x[a], dy = Q.y[b] - Q.y[a], l = Math.hypot(dx, dy) || 1; return [dx / l, dy / l]; }); Q.TS = ts(2); Q.TL = ts(9); }
 const dt = GB && S.delta ? Array.from({length: N}, (_, i) => S.delta[i] ?? 0) : null;
 const rate = dt ? dt.map((_, i) => { const a = Math.max(0, i - 4), b = Math.min(N - 1, i + 4); return (dt[b] - dt[a]) / ((b - a) * STEP) * 100; }) : null;
 const RATEMAX = rate ? (() => { const s = rate.map(Math.abs).sort((a, b) => a - b); return s[Math.floor(s.length * .95)] || 1; })() : 1;
 const onsets = Q => { const o = []; for (let i = 1; i < N; i++) if (Q.br[i] >= .1 && Q.br[i - 1] < .1) o.push(i); return o; };
 const ON = {A: onsets(GA), B: GB ? onsets(GB) : []};
 const LEN = (N - 1) * STEP;
 // braking zones: 60 m before lap A's brake point to 40 m after it is back on full throttle; close zones merge
 const ZONES = [];
 for (const i of ON.A) { let j = i; while (j < N - 1 && !(GA.th[j] >= .9 && GA.br[j] < .05) && (j - i) * STEP < 380) j++;
  const d0 = Math.max(0, i * STEP - 60), d1 = Math.min(LEN, j * STEP + 40);
  if (ZONES.length && d0 <= ZONES[ZONES.length - 1].d1) { ZONES[ZONES.length - 1].d1 = Math.max(ZONES[ZONES.length - 1].d1, d1); continue; }
  ZONES.push({i, d0, d1}); }
 const cross = (Q, i) => { const a = Q.br[i - 1] || 0, b = Q.br[i] || 0; return (i - 1 + clamp((.1 - a) / ((b - a) || 1), 0, 1)) * STEP; };  // 10 % crossing between samples
 for (const z of ZONES) {
  z.i0 = Math.round(z.d0 / STEP); z.i1 = Math.min(N - 1, Math.round(z.d1 / STEP));
  const near = ON.B.filter(i => Math.abs(i - z.i) <= 16).sort((a, b) => Math.abs(a - z.i) - Math.abs(b - z.i))[0];
  z.bpA = cross(GA, z.i); z.bpB = near == null ? null : cross(GB, near);
  let mB = 1e9; if (GB) for (let k = z.i0; k <= z.i1; k++) mB = Math.min(mB, GB.v[k]); z.minB = mB;
 }
 const all = GB ? [...GA.v, ...GB.v] : GA.v, VMIN = Math.min(...all), VMAX = Math.max(...all);
 let dm = .05; if (dt) for (const v of dt) dm = Math.max(dm, Math.abs(v));
 const dst = dm > .5 ? .25 : dm > .2 ? .1 : .05;
 const SEC = [0, ...(S.map ? S.map.sectors || [] : [])].map(d => Math.round(d / STEP)).filter(i => i < N);
 LA.G = {N, STEP, LEN, A: GA, B: GB, nL, HW, EL, ER, BX, EXT: Math.max(BX.x1 - BX.x0, BX.y1 - BX.y0), dt, rate, RATEMAX, ON, ZONES, SEC,
  SPD: [Math.floor((VMIN - 10) / 50) * 50, Math.ceil((VMAX + 5) / 50) * 50], DMAX: Math.ceil(dm * 1.1 / dst) * dst, GMAX: Math.max(1, ...GA.g, ...(GB ? GB.g : []))};
}

// position and heading of a lap at a time; the long tangent gives the follow camera a calm heading
export function carAt(Q, s, wide) {
 const G = LA.G, ts = Q.t; let lo = 0, hi = G.N - 1; while (hi - lo > 1) { const m = (lo + hi) >> 1; if (ts[m] != null && ts[m] <= s) lo = m; else hi = m; }
 const f = clamp((s - ts[lo]) / ((ts[hi] - ts[lo]) || 1), 0, 1), T = wide ? Q.TL : Q.TS, hx = T[lo][0] + (T[hi][0] - T[lo][0]) * f, hy = T[lo][1] + (T[hi][1] - T[lo][1]) * f, l = Math.hypot(hx, hy) || 1;
 return {x: Q.x[lo] + (Q.x[hi] - Q.x[lo]) * f, y: Q.y[lo] + (Q.y[hi] - Q.y[lo]) * f, fx: hx / l, fy: hy / l};
}

// ---------- map camera: a flat top view that pans, zooms and turns; follow keeps the car pointing up ----------
export const cam = {x: 0, y: 0, s: 1, r: 0}, goal = {...cam};

export function proj() {
 const [W, H] = LA.wh, c = Math.cos(cam.r), sn = Math.sin(cam.r), s = cam.s;
 return {P: (x, y) => { const dx = x - cam.x, dy = y - cam.y; return [W / 2 + (dx * c + dy * sn) * s, H / 2 - (-dx * sn + dy * c) * s]; },
  ground: (sx, sy) => { const u = (sx - W / 2) / s, v = -(sy - H / 2) / s; return [cam.x + u * c - v * sn, cam.y + u * sn + v * c]; }};
}

export function fitGoal() {
 const G = LA.G, [W, H] = LA.wh; if (!G) return;
 const s = Math.min((W - 120) / ((G.BX.x1 - G.BX.x0) || 1), (H - 70) / ((G.BX.y1 - G.BX.y0) || 1));
 Object.assign(goal, {x: (G.BX.x0 + G.BX.x1) / 2, y: (G.BX.y0 + G.BX.y1) / 2, s: Math.max(.01, s), r: 0}); LA.fitS = goal.s;
}

export function fitNow() { fitGoal(); Object.assign(cam, goal); LA.dirty = true; }

export function setFollow(on) { S.follow = on; $("follow").setAttribute("aria-pressed", on); if (on) goal.s = Math.max(goal.s, Math.min(...LA.wh) / 320); }

export const zoomAt = (f, sx, sy) => {
 const {ground} = proj(), g0 = sx != null ? ground(sx, sy) : null;
 goal.s = clamp(goal.s * f, LA.fitS * .6, 40);
 if (g0 && !S.follow) { cam.s = goal.s; const g1 = proj().ground(sx, sy); goal.x = cam.x += g0[0] - g1[0]; goal.y = cam.y += g0[1] - g1[1]; }
 LA.dirty = true;
};

// ---------- map drawing (canvas) ----------
export function drawMap() {
 const g = LA.ctx, K = LA.K, G = LA.G, [W, H] = LA.wh; if (!g) return;
 g.clearRect(0, 0, W, H); g.lineCap = "round"; g.lineJoin = "round";
 if (!G) { $("cgap").textContent = ""; return; }
 const {P} = proj(), N = G.N, nL = G.nL, z = clamp(Math.sqrt(cam.s / LA.fitS), 1, 2.2);
 const quad = (a, b, c, d) => { g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.lineTo(c[0], c[1]); g.lineTo(d[0], d[1]); g.closePath(); };
 const across = (x, y, i, half) => { const n = nL[i]; return [P(x + n[0] * half, y + n[1] * half), P(x - n[0] * half, y - n[1] * half)]; };
 // ground dots every 100 m, once they are far enough apart to read as a grid
 if (cam.s * 100 >= 14) { g.fillStyle = K.bd; for (let x = Math.floor((G.BX.x0 - 300) / 100) * 100; x <= G.BX.x1 + 300; x += 100) for (let y = Math.floor((G.BX.y0 - 300) / 100) * 100; y <= G.BX.y1 + 300; y += 100) { const p = P(x, y); if (p[0] > -4 && p[0] < W + 4 && p[1] > -4 && p[1] < H + 4) g.fillRect(p[0] - .7, p[1] - .7, 1.4, 1.4); } }
 // road surface at real width
 const L = G.EL.map(p => P(p[0], p[1])), R = G.ER.map(p => P(p[0], p[1]));
 g.lineWidth = 1; g.fillStyle = g.strokeStyle = K.l3;
 for (let i = 0; i < N - 1; i++) { quad(L[i], L[i + 1], R[i + 1], R[i]); g.fill(); g.stroke(); }
 // paint on the tarmac: braking (pedals) or where time changes hands (gain/loss); each 5 m piece blends start to end shade
 const paintAt = i => {
  if (S.line === "pedals" || !G.rate) { const br = G.A.br[i]; return br > .05 ? [K.red, .12 + .4 * br] : null; }
  const r = G.rate[i], m = Math.abs(r) / G.RATEMAX; return m > .08 ? [r < 0 ? K.pri : K.ter, clamp(.1 + .5 * m, 0, .6)] : null;
 };
 const PA = Array.from({length: N}, (_, i) => paintAt(i));
 for (let i = 0; i < N - 1; i++) {
  const p0 = PA[i], p1 = PA[i + 1]; if (!p0 && !p1) continue;
  const col = (p0 || p1)[0], c0 = mix(K.l3, col, p0 && p0[0] === col ? p0[1] : 0), c1 = mix(K.l3, col, p1 && p1[0] === col ? p1[1] : 0);
  const m0 = [(L[i][0] + R[i][0]) / 2, (L[i][1] + R[i][1]) / 2], m1 = [(L[i + 1][0] + R[i + 1][0]) / 2, (L[i + 1][1] + R[i + 1][1]) / 2];
  const gr = g.createLinearGradient(m0[0], m0[1], m1[0], m1[1]); gr.addColorStop(0, c0); gr.addColorStop(1, c1);
  g.fillStyle = g.strokeStyle = gr; quad(L[i], L[i + 1], R[i + 1], R[i]); g.fill(); g.stroke();
 }
 g.strokeStyle = K.bd; g.lineWidth = 1;
 for (const E of [L, R]) { g.beginPath(); E.forEach((p, k) => k ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])); g.stroke(); }
 // start/finish and sector lines across the road
 const labels = [];
 G.SEC.forEach((i, s) => { g.strokeStyle = K.t1; g.lineWidth = (s ? 1.4 : 2.6) * z; g.beginPath(); g.moveTo(L[i][0], L[i][1]); g.lineTo(R[i][0], R[i][1]); g.stroke(); labels.push([P(G.EL[i][0] + nL[i][0] * 26, G.EL[i][1] + nL[i][1] * 26), s]); });
 // each driven line is stroked in runs of one colour, so joins are clean
 const runs = (Q, styleOf, w) => { let run = null;
  const flush = () => { if (run && run.pts.length > 1) { g.strokeStyle = run.c; g.globalAlpha = run.a; g.lineWidth = w * z; g.beginPath(); run.pts.forEach((p, k) => k ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])); g.stroke(); } run = null; };
  for (let i = 0; i < N; i++) { const p = P(Q.x[i], Q.y[i]), [c, a0] = styleOf(Math.min(i, N - 2)), a = Math.round(a0 * 5) / 5;
   if (!run || run.c !== c || run.a !== a || run.pts.length > 14) { const last = run && run.pts[run.pts.length - 1]; flush(); run = {c, a, pts: last ? [last] : []}; }
   run.pts.push(p); }
  flush(); g.globalAlpha = 1; };
 const brakeMarks = (Q, list, col, w) => list.forEach(i => { const [a, b] = across(Q.x[i], Q.y[i], i, G.HW[i] * .9); g.strokeStyle = col; g.lineWidth = w * z; g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke(); });
 if (G.B) { runs(G.B, () => [K.t3, .85], 1.4); brakeMarks(G.B, G.ON.B, K.t3, 2); }
 runs(G.A, i => { const br = G.A.br[i], th = G.A.th[i]; return br > .05 ? [K.red, .55 + .45 * br] : th > .05 ? [K.green, .35 + .65 * th] : [K.t3, .7]; }, 2.6);
 brakeMarks(G.A, G.ON.A, K.red, 2.6);
 // sector names, with lap A's sector time against lap B's
 g.font = `600 12.5px ${getComputedStyle(document.body).fontFamily}`; g.textAlign = "left";
 const sa = S.lapA.trace.sectors || [], sb = S.lapB ? S.lapB.trace.sectors || [] : [];
 for (const [o, s] of labels) { const d = sa[s] != null && sb[s] != null ? sa[s] - sb[s] : null, name = t("sector.n", {n: s + 1}), dl = d == null ? "" : "  " + signed(d, 3);
  const w1 = g.measureText(name).width, w2 = g.measureText(dl).width, x0 = o[0] - (w1 + w2) / 2;
  g.fillStyle = K.t2; g.fillText(name, x0, o[1] + 4); if (dl) { g.fillStyle = d < 0 ? K.pri : K.ter; g.fillText(dl, x0 + w1, o[1] + 4); } }
 // the cars: a circle each with its letter, lap A light and lap B grey, ringed in the tile
 // colour; both at the same time in the lap
 const dot = (Q, col, r, ch) => { const c = carAt(Q, S.t), q = P(c.x, c.y); g.beginPath(); g.arc(q[0], q[1], r, 0, Math.PI * 2); g.lineWidth = 3; g.strokeStyle = K.l1; g.stroke(); g.fillStyle = col; g.fill();
  g.font = `600 11.5px ${getComputedStyle(document.body).fontFamily}`; g.textAlign = "center"; g.textBaseline = "middle"; if (ch) { g.fillStyle = K.l1; g.fillText(ch, q[0], q[1] + .5); } g.textBaseline = "alphabetic"; return q; };
 if (G.B) dot(G.B, K.lapB, 9, "B");
 const pa = G.B ? dot(G.A, K.lapA, 9, "A") : dot(G.A, K.lapA, 7.5, null);
 // the gap to lap B, just above lap A's circle
 const el = $("cgap");
 if (G.dt && pa[1] > 0 && pa[1] < H) { const gp = G.dt[idxNow()]; el.textContent = signed(gp, 3) + " s"; el.style.color = gp < 0 ? "var(--pri)" : gp > 0 ? "var(--ter)" : "var(--t2)"; el.style.left = pa[0] + "px"; el.style.top = (pa[1] - 16) + "px"; }
 else el.textContent = "";
}

export function initMap() {
 const cvs = $("map");
 cvs.addEventListener("contextmenu", e => e.preventDefault());
 cvs.addEventListener("pointerdown", e => { const r = cvs.getBoundingClientRect(); LA.drag = {turn: e.button === 2 || e.shiftKey, x: e.clientX, g: proj().ground(e.clientX - r.left, e.clientY - r.top), r: goal.r}; cvs.setPointerCapture(e.pointerId); cvs.classList.add("drag"); });
 cvs.addEventListener("pointermove", e => { const d = LA.drag; if (!d) return; const r = cvs.getBoundingClientRect();
  if (d.turn) { goal.r = cam.r = d.r - (e.clientX - d.x) * .006; LA.dirty = true; return; }
  if (S.follow) setFollow(false);
  const g1 = proj().ground(e.clientX - r.left, e.clientY - r.top); goal.x = cam.x += d.g[0] - g1[0]; goal.y = cam.y += d.g[1] - g1[1]; LA.dirty = true; });
 const up = () => { LA.drag = null; cvs.classList.remove("drag"); };
 cvs.addEventListener("pointerup", up); cvs.addEventListener("pointercancel", up);
 cvs.addEventListener("wheel", e => { e.preventDefault(); const r = cvs.getBoundingClientRect(); zoomAt(e.deltaY < 0 ? 1.18 : 1 / 1.18, e.clientX - r.left, e.clientY - r.top); }, {passive: false});
 cvs.addEventListener("dblclick", () => { setFollow(false); fitGoal(); });
 $("zin").onclick = () => zoomAt(1.35); $("zout").onclick = () => zoomAt(1 / 1.35);
 $("fit").onclick = () => { setFollow(false); fitGoal(); };
 $("follow").onclick = () => { setFollow(!S.follow); if (!S.follow) fitGoal(); };
 new ResizeObserver(() => { const r = $("mapt").getBoundingClientRect(), dpr = devicePixelRatio || 1; if (r.width < 10 || r.height < 10) return;
  LA.wh = [r.width, r.height]; cvs.width = Math.round(r.width * dpr); cvs.height = Math.round(r.height * dpr); LA.ctx = cvs.getContext("2d"); LA.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (S.follow) goal.s = Math.max(goal.s, Math.min(...LA.wh) / 320); else fitNow(); LA.dirty = true; }).observe($("mapt"));
}
