import {$, el, clamp, signed, fmtLap, fx, cv, UNIT, t, S, LA, timeAt, lapT, dA, dB, at, lapLabel} from "./state.js";
import {carAt, cam, goal, fitGoal, setFollow, drawMap} from "./map.js";

// ---------- trace deck: delta, speed, throttle, brake and gear on one distance axis, centred on the cursor ----------
export const whole = () => !LA.G || LA.win >= LA.G.LEN / 2;
export function deckWindow() { if (whole()) return [0, LA.G ? LA.G.LEN : 1]; const c = dA(); return [c - LA.win, c + LA.win]; }

export function deckLayout() {
 const [, DH] = LA.dw, gap = 8, axis = 16, avail = DH - axis - gap;
 let lanes = [["d", 100], ["v", 170], ["th", 110], ["br", 110], ["g", 66]].filter(l => (LA.G && LA.G.dt) || l[0] !== "d");
 const sum = ls => ls.reduce((s, l) => s + l[1], 0) + gap * (ls.length - 1);
 if (sum(lanes) > avail) lanes = lanes.filter(l => l[0] !== "g");  // gear goes first on short screens
 const k = (avail - gap * (lanes.length - 1)) / lanes.reduce((s, l) => s + l[1], 0), out = {lanes: {}, axisY: DH - axis}; let y = 2;
 for (const [key, h] of lanes) { out.lanes[key] = [y, h * k]; y += h * k + gap; }
 return out;
}

const laneName = k => k === "d" ? t("trace.delta", {lap: lapLabel(S.lapB)}) : t({v: "trace.speed", th: "trace.throttle", br: "trace.brake", g: "trace.gear"}[k]);

export function drawDeck() {
 const g = LA.dctx, K = LA.K, G = LA.G, [DW, DH] = LA.dw; if (!g) return;
 g.clearRect(0, 0, DW, DH); if (DW < 20 || !G) return;
 g.lineJoin = "round"; g.lineCap = "round";
 const font = getComputedStyle(document.body).fontFamily, Lo = deckLayout(), [d0, d1] = deckWindow(), X = d => (d - d0) / (d1 - d0) * DW, STEP = G.STEP;
 const lo = clamp(Math.floor(d0 / STEP) - 1, 0, G.N - 1), hi = clamp(Math.ceil(d1 / STEP) + 1, 0, G.N - 1), PADT = 20, PADB = 6, secs = S.map ? S.map.sectors || [] : [];
 // each lane is its own panel with its name, a fixed scale and quiet gridlines
 const panel = (key, ticks, fmt) => {
  const [y, h] = Lo.lanes[key], py = y + PADT, ph = h - PADT - PADB;
  g.fillStyle = K.l2; g.beginPath(); g.roundRect(0, y, DW, h, 6); g.fill();
  g.font = "600 11px " + font; g.fillStyle = K.t2; g.textAlign = "left"; g.fillText(laneName(key), 8, y + 14);
  g.font = "11px " + font; g.textAlign = "right";
  for (const [v, yy] of ticks) { g.strokeStyle = K.l3; g.lineWidth = 1; g.beginPath(); g.moveTo(0, yy + .5); g.lineTo(DW, yy + .5); g.stroke(); g.fillStyle = K.t3; g.fillText(fmt(v), DW - 6, yy - 3); }
  g.strokeStyle = K.l3; for (const d of secs) if (d > d0 && d < d1) { g.beginPath(); g.moveTo(X(d), py); g.lineTo(X(d), py + ph); g.stroke(); }
  return [y, h];
 };
 const clip = (y, h, fn) => { g.save(); g.beginPath(); g.rect(0, y - 3, DW, h + 6); g.clip(); fn(); g.restore(); };
 const series = (arr, Y, col, w, dash) => { g.strokeStyle = col; g.lineWidth = w; g.setLineDash(dash || []); g.beginPath(); for (let i = lo; i <= hi; i++) { const px = X(i * STEP), py = Y(arr[i]); i === lo ? g.moveTo(px, py) : g.lineTo(px, py); } g.stroke(); g.setLineDash([]); };
 // Δ: fixed scale for the lap, coloured by its trend
 if (Lo.lanes.d) {
  const [y0, h0] = Lo.lanes.d, py = y0 + PADT, ph = h0 - PADT - PADB, Y = v => py + ph / 2 - v / G.DMAX * ph / 2;
  const [y, h] = panel("d", [[G.DMAX, Y(G.DMAX)], [0, Y(0)], [-G.DMAX, Y(-G.DMAX)]], v => v ? signed(v, 2) : "0");
  const colAt = i => Math.abs(G.rate[i]) < G.RATEMAX * .12 ? K.t1 : G.rate[i] < 0 ? K.pri : K.ter;
  clip(y, h, () => { g.lineWidth = 2; let i0 = lo; while (i0 < hi) { const c = colAt(i0); let i1 = i0; while (i1 < hi && colAt(i1) === c) i1++; g.strokeStyle = c; g.beginPath(); for (let i = i0; i <= i1; i++) { const px = X(i * STEP), qy = Y(G.dt[i]); i === i0 ? g.moveTo(px, qy) : g.lineTo(px, qy); } g.stroke(); i0 = i1; } });
 }
 // speed: lap A light, lap B grey dashed; each zone's minimum labelled
 if (Lo.lanes.v) {
  const [y0, h0] = Lo.lanes.v, py = y0 + PADT, ph = h0 - PADT - PADB, Y = v => py + ph - (v - G.SPD[0]) / (G.SPD[1] - G.SPD[0]) * ph, tk = [];
  for (let v = G.SPD[0] + 50; v < G.SPD[1]; v += 50) tk.push([v, Y(v)]);
  const [y, h] = panel("v", tk, v => v);
  clip(y, h, () => { if (G.B) series(G.B.v, Y, K.t3, 1.5, [5, 4]); series(G.A.v, Y, K.t1, 2); });
  for (const z of G.ZONES) {
   let im = z.i0; for (let k = z.i0; k <= z.i1; k++) if (G.A.v[k] < G.A.v[im]) im = k;
   const px = X(im * STEP); if (px < 20 || px > DW - 40) continue;
   const qy = Y(G.A.v[im]), tA = String(Math.round(G.A.v[im])), tB = G.B ? " · " + Math.round(z.minB) : "";
   g.font = "600 11.5px " + font; const w1 = g.measureText(tA).width; g.font = "11.5px " + font; const w2 = g.measureText(tB).width, x0 = px - (w1 + w2) / 2, ty = Math.min(y + h - 2, qy + 18);
   g.textAlign = "left"; g.font = "600 11.5px " + font; g.fillStyle = K.t1; g.fillText(tA, x0, ty); if (tB) { g.font = "11.5px " + font; g.fillStyle = K.t3; g.fillText(tB, x0 + w1, ty); }
  }
 }
 // throttle and brake: lap A in its colour, lap B grey dashed; brake points ticked on the brake lane
 for (const [key, col] of [["th", K.green], ["br", K.red]]) {
  if (!Lo.lanes[key]) continue;
  const [y0, h0] = Lo.lanes[key], py = y0 + PADT, ph = h0 - PADT - PADB, Y = v => py + ph - clamp(v, 0, 1) * ph;
  const [y, h] = panel(key, [[.5, Y(.5)]], () => "50 %");
  clip(y, h, () => { if (G.B) series(G.B[key], Y, K.t3, 1.4, [5, 4]); series(G.A[key], Y, col, 2); });
  if (key === "br") for (const z of G.ZONES) { const tick = (d, c) => { const px = X(d); if (px < 0 || px > DW) return; g.strokeStyle = c; g.lineWidth = 2; g.beginPath(); g.moveTo(px, py + ph - 6); g.lineTo(px, py + ph + 4); g.stroke(); }; if (z.bpB != null) tick(z.bpB, K.t3); tick(z.bpA, K.t1); }
 }
 // gear, stepped
 if (Lo.lanes.g) {
  const [y0, h0] = Lo.lanes.g, py = y0 + PADT, ph = h0 - PADT - PADB, Y = v => py + ph - ((v || 1) - 1) / Math.max(1, G.GMAX - 1) * ph;
  const [y, h] = panel("g", [], v => v);
  const step = (Q, c, w, dash) => { g.strokeStyle = c; g.lineWidth = w; g.setLineDash(dash || []); g.beginPath(); for (let i = lo; i <= hi; i++) { const px = X(i * STEP), qy = Y(Q.g[i]); if (i === lo) g.moveTo(px, qy); else { g.lineTo(px, Y(Q.g[i - 1])); g.lineTo(px, qy); } } g.stroke(); g.setLineDash([]); };
  clip(y, h, () => { if (G.B) step(G.B, K.t3, 1.4, [5, 4]); step(G.A, K.t1, 1.8); });
 }
 // distance axis: sector names for the whole lap, metres from the cursor otherwise
 g.font = "11px " + font; g.fillStyle = K.t3; g.textAlign = "center";
 if (whole()) { const b = [0, ...secs, G.LEN]; for (let s = 0; s < b.length - 1; s++) g.fillText(t("sector.n", {n: s + 1}), X((b[s] + b[s + 1]) / 2), DH - 2); }
 else { const c = dA(), px = DW / (2 * LA.win), st = [25, 50, 100, 200, 500].find(v => v * px >= 70) || 500; for (let r = -Math.floor(LA.win / st) * st; r <= LA.win; r += st) { const qx = X(c + r); if (r && qx > 26 && qx < DW - 26) g.fillText((r > 0 ? "+" : "−") + Math.abs(r) + " m", qx, DH - 2); } }
 // the cursor, and the hover line
 const bot = Lo.axisY - 4, xc = X(dA());
 g.strokeStyle = K.t1; g.lineWidth = 1.5; g.beginPath(); g.moveTo(xc, 0); g.lineTo(xc, bot); g.stroke();
 if (LA.hov != null) { const xh = X(LA.hov); g.strokeStyle = K.t2; g.lineWidth = 1; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(xh, 0); g.lineTo(xh, bot); g.stroke(); g.setLineDash([]); }
}

// values at the pointer: one small tag per lane, lap A then lap B
export function readout() {
 const el = $("dread"), G = LA.G; el.replaceChildren(); if (LA.hov == null || !G) return;
 const DW = LA.dw[0], Lo = deckLayout(), [d0, d1] = deckWindow(), x = (LA.hov - d0) / (d1 - d0) * DW, i = clamp(Math.round(LA.hov / G.STEP), 0, G.N - 1), left = x > DW - 120;
 const tag = (lane, a, b) => { if (!Lo.lanes[lane]) return; const [y, h] = Lo.lanes[lane], s = document.createElement("span"); s.textContent = a; if (G.B && b != null) { const k = document.createElement("i"); k.textContent = " " + b; s.append(k); }
  s.style.top = (y + h / 2) + "px"; if (left) s.style.right = (DW - x + 8) + "px"; else s.style.left = (x + 8) + "px"; el.append(s); };
 if (G.dt) tag("d", signed(G.dt[i], 3) + " s", null);
 tag("v", Math.round(G.A.v[i]) + " " + UNIT.speed, G.B && Math.round(G.B.v[i]));
 tag("th", Math.round(G.A.th[i] * 100) + " %", G.B && Math.round(G.B.th[i] * 100));
 tag("br", Math.round(G.A.br[i] * 100) + " %", G.B && Math.round(G.B.br[i] * 100));
 tag("g", t("dock.gear") + " " + G.A.g[i], G.B && G.B.g[i]);
}

export function setDist(d) { if (!S.lapA || !LA.G) return; S.t = clamp(timeAt(S.lapA, clamp(d, 0, LA.G.LEN - 1)) ?? 0, 0, lapT()); setPlay(false); LA.dirty = true; }

// ---------- bottom bar: mirrored live pedals, play, and the lap as one bar ----------
const PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z"/></svg>';
const PAUSE = '<svg viewBox="0 0 24 24"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';

function sideCells(cl) {
 const L = cl === "a" ? S.lapA : S.lapB;
 const who = `<div class="cell who" data-tip="${fmtLap(L && L.time)}">${!S.lapB ? lapLabel(L) : cl === "a" ? `<span class="ab a">A</span>${lapLabel(L)}` : `${lapLabel(L)}<span class="ab b">B</span>`}</div>`;
 const thr = `<div class="cell pd"><span class="pv" id="thv${cl}"></span><span class="bar"><i id="th${cl}" style="background:var(--green)"></i></span></div>`;
 const brk = `<div class="cell pd brk"><span class="bar"><i id="br${cl}" style="background:var(--red)"></i></span><span class="pv" id="brv${cl}" style="text-align:right"></span></div>`;
 const gear = `<div class="cell gr" data-tip="${t("tip.gear")}"><small>${t("dock.gear")}</small><span id="gr${cl}"></span></div>`;
 const spd = `<div class="cell sp"><span id="sp${cl}"></span><small>${UNIT.speed}</small></div>`;
 return cl === "a" ? who + thr + brk + gear + spd : spd + gear + thr + brk + who;
}

export function buildDock() {
 $("sideA").innerHTML = S.lapA ? sideCells("a") : "";
 $("sideB").innerHTML = S.lapB ? sideCells("b") : "";
 $("sideB").classList.toggle("off", !S.lapB);
 $("pp").disabled = !S.lapA;
 // the bar runs in lap A's time, so the sector marks sit where lap A crossed each line
 const rail = $("rail"); rail.querySelectorAll(".sx,.sn").forEach(e => e.remove());
 $("tend").textContent = S.lapA ? fmtLap(lapT()) : "";
 if (!S.lapA) return;
 const T = lapT(), b = [0, ...(S.map ? S.map.sectors || [] : []).map(d => timeAt(S.lapA, d) ?? 0), T], pc = s => clamp(s / T, 0, 1) * 100 + "%";
 for (let s = 0; s < b.length - 1; s++) {
  if (s) { const x = el("span", {class: "sx"}); x.style.left = pc(b[s]); rail.append(x); }
  const n = el("span", {class: "sn"}, t("sector.n", {n: s + 1})); n.style.left = pc((b[s] + b[s + 1]) / 2); rail.append(n);
 }
}

export function pedals() {
 const d = dA();
 [["a", S.lapA, d], ...(S.lapB ? [["b", S.lapB, dB(d)]] : [])].forEach(([cl, L, dd]) => {
  if (!L || !$("th" + cl)) return;
  const th = clamp((at(L, "throttle", dd) || 0) * 100, 0, 100), br = clamp((at(L, "brake", dd) || 0) * 100, 0, 100);
  $("th" + cl).style.width = th + "%"; $("thv" + cl).textContent = th.toFixed(1) + "%";
  $("br" + cl).style.width = br + "%"; $("brv" + cl).textContent = br.toFixed(1) + "%";
  $("sp" + cl).textContent = fx(cv("speed", at(L, "speed", dd)), 1); $("gr" + cl).textContent = Math.round(at(L, "gear", dd) ?? 0);
 });
}

export function playerUI() {
 const T = lapT(), p = T ? clamp(S.t / T, 0, 1) * 100 : 0;
 $("phead").style.left = p + "%"; $("tfill").style.width = p + "%"; $("scrub").setAttribute("aria-valuenow", p.toFixed(0));
 $("tnow").textContent = T ? fmtLap(S.t) : "";
 $("pp").innerHTML = S.play ? PAUSE : PLAY; $("pp").setAttribute("aria-label", S.play ? t("play.pause") : t("play.play")); $("pp").title = $("pp").getAttribute("aria-label");
}

export function setPlay(on) { S.play = !!on && !!S.lapA; LA.last = 0; LA.dirty = true; if ($("pp")) playerUI(); }

export function initTraces() {
 const c = $("deckc"), atX = e => e.clientX - c.getBoundingClientRect().left;
 const dOf = x => { const [d0, d1] = deckWindow(); return d0 + x / LA.dw[0] * (d1 - d0); };
 c.addEventListener("pointerdown", e => { c.setPointerCapture(e.pointerId); setDist(dOf(atX(e))); LA.hov = null; readout(); });
 c.addEventListener("pointermove", e => { if (e.buttons & 1) { setDist(dOf(atX(e))); return; } LA.hov = clamp(dOf(atX(e)), 0, LA.G ? LA.G.LEN : 0); readout(); LA.dirty = true; });
 c.addEventListener("pointerleave", () => { LA.hov = null; readout(); LA.dirty = true; });
 c.addEventListener("wheel", e => { e.preventDefault(); if (!LA.G) return; LA.win = clamp((whole() ? LA.G.LEN / 2 : LA.win) * (e.deltaY > 0 ? 1.2 : 1 / 1.2), 50, LA.G.LEN / 2); LA.hov = null; readout(); LA.dirty = true; }, {passive: false});
 new ResizeObserver(() => { const r = c.getBoundingClientRect(), dpr = devicePixelRatio || 1; LA.dw = [r.width, r.height]; c.width = Math.max(1, Math.round(r.width * dpr)); c.height = Math.max(1, Math.round(r.height * dpr)); LA.dctx = c.getContext("2d"); LA.dctx.setTransform(dpr, 0, 0, dpr, 0, 0); LA.dirty = true; }).observe(c);

 $("pp").onclick = () => setPlay(!S.play);
 const sc = $("scrub"), to = e => { const r = $("rail").getBoundingClientRect(); S.t = clamp((e.clientX - r.left) / r.width, 0, .9999) * lapT(); LA.dirty = true; };
 sc.addEventListener("pointerdown", e => { if (!S.lapA) return; sc.setPointerCapture(e.pointerId); setPlay(false); to(e); });
 sc.addEventListener("pointermove", e => { if (e.buttons & 1 && S.lapA) to(e); });

 // keys on the analysis screen: ← → a second (Shift: a tenth), 1 2 3 deck window, F follow
 document.addEventListener("keydown", e => {
  if (S.screen !== "analysis" || !S.lapA || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); setPlay(false); S.t = clamp(S.t + (e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? .1 : 1), 0, lapT() - .001); LA.dirty = true; }
  else if (e.key === "1" || e.key === "2" || e.key === "3") { LA.win = {1: 75, 2: 150, 3: 1e9}[e.key]; LA.dirty = true; }
  else if (e.key === "f" || e.key === "F") { setFollow(!S.follow); if (!S.follow) fitGoal(); }
 });
}

// ---------- one loop: playback, the camera easing to its goal, and redraws when something changed ----------
function lerpAng(a, b, f) { let d = b - a; while (d > Math.PI) d -= 2 * Math.PI; while (d < -Math.PI) d += 2 * Math.PI; return a + d * f; }

function loop(now) {
 requestAnimationFrame(loop);  // first, so one bad frame never stops the loop
 const dts = LA.last ? Math.min(.1, (now - LA.last) / 1000) : 0; LA.last = now;
 if (S.screen !== "analysis") return;
 if (S.play && S.lapA) { S.t += dts; if (S.t >= lapT()) S.t -= lapT(); LA.dirty = true; }
 if (S.follow && LA.G) {
  const c = carAt(LA.G.A, S.t, true), h = Math.atan2(c.fy, c.fx), ahead = Math.min(...LA.wh) * .2 / goal.s;
  goal.r = h - Math.PI / 2; goal.x = c.x + c.fx * ahead; goal.y = c.y + c.fy * ahead;
  if (S.play) { cam.x = goal.x; cam.y = goal.y; }  // while playing the camera rides with the car
 }
 const f = 1 - Math.pow(.0009, dts || .016); let moving = false;
 for (const k of ["x", "y", "s"]) { const d = goal[k] - cam[k]; if (Math.abs(d) > (k === "s" ? cam.s * 1e-3 : .05)) { cam[k] += d * f; moving = true; } else cam[k] = goal[k]; }
 const nr = lerpAng(cam.r, goal.r, f); if (Math.abs(nr - cam.r) > 1e-5) { cam.r = nr; moving = true; } else cam.r = goal.r;
 if (moving || LA.dirty) { drawMap(); if (LA.dirty) { drawDeck(); readout(); pedals(); playerUI(); } LA.dirty = false; }
}

export function initAnalysisLoop() {
 requestAnimationFrame(loop);
}
