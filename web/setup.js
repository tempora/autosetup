import {$, el, sv, svgBox, css, clamp, fx, signed, api, t, showError, S, pills, refreshThumbs, baseStint, stintById, stintNo} from "./state.js";
import {CARS, family, drawSide, drawTop} from "./cars.js";

// ---------- setup: balance, car, dampers; next setup docked on the right ----------
let proposalTimer = null, proposalRunning = false, proposalAgain = false;
export const scheduleProposal = () => { clearTimeout(proposalTimer); proposalTimer = setTimeout(loadProposal, 400); };

export async function loadProposal() {
 clearTimeout(proposalTimer);
 if (proposalRunning) { proposalAgain = true; return; }
 const st = baseStint();
 if (!st) { S.proposal = null; if (S.screen === "setup") renderSetup(); return; }
 proposalRunning = true; setBusy(true);
 let pr = null, failed = null;
 try { pr = await api("/api/proposal?stint=" + st.id); } catch (e) { failed = e; } finally { proposalRunning = false; }
 if (proposalAgain) { proposalAgain = false; return loadProposal(); }
 setBusy(false);
 if (failed) showError(failed.message);
 S.proposal = pr;
 if (S.screen === "setup") renderSetup();
}

function setBusy(on) { S.proposalBusy = on; const l = document.querySelector("#panel .list"); if (l) l.classList.toggle("busy", on); const w = $("write"); if (w && on) w.disabled = true; setStatus(on ? t("setup.working") : (S.status || "")); }
function setStatus(text, error) { const s = $("status"); if (!s) return; s.textContent = text || ""; s.classList.toggle("error", !!error); }

const PHASES = [["braking", "phase.braking"], ["entry", "phase.entry"], ["mid", "phase.mid"], ["exit", "phase.exit"]], BANDS = [["high", "band.high"], ["low", "band.low"]];

function balance(box, plan) {
 const tg = plan && plan.target || {}, cells = tg.cells || {}, tol = tg.tolerance || {}, meas = (plan && plan.measured && plan.measured.cells) || {}, pred = (plan && plan.predicted) || {};
 if (!Object.keys(cells).length) { box.append(el("div", {class: "empty"}, t("balance.none"))); return; }
 const M = 2.5, pct = v => ((clamp(v, -M, M) + M) / (2 * M) * 100).toFixed(2) + "%";
 const word = (v, band) => Math.abs(v) <= band ? t("balance.ontarget") : t(v > 0 ? "balance.us" : "balance.os", {v: Math.abs(v).toFixed(1)});
 const cls = (v, band) => Math.abs(v) <= band ? "" : v > 0 ? "us" : "os";
 const wrap = el("div", {class: "bx"});
 const ax = el("div", {class: "bx-axis"}), ends = el("div", {class: "bx-ends"});
 ends.append(el("span", {}, t("balance.left")), el("span", {}, t("balance.mid")), el("span", {}, t("balance.right")));
 ax.append(el("span"), ends, el("span")); wrap.append(ax);
 for (const [band, bl] of BANDS) {
  wrap.append(el("div", {class: "bx-grp"}, t(bl)));
  for (const [ph, pl] of PHASES) {
   const c = `${ph}_${band}`, target = cells[c], m = meas[c] ? meas[c][0] : null, p = pred[c] ? pred[c][0] : null, B = tol[c] || 0.15;
   if (ph === "braking" && target == null && m == null) continue;  // no braking zone measured in this band
   const row = el("div", {class: "bx-row"}), sc = el("div", {class: "bx-scale"}), txt = el("span", {class: "bx-txt"});
   row.append(el("span", {class: "bx-ph"}, t(pl)), sc, txt);
   sc.innerHTML = `<span class="bx-band" style="left:${pct(-B)};right:calc(100% - ${pct(B)})"></span><span class="bx-mid"></span>`;
   if (target != null && m != null) {
    const now = m - target, aft = p != null ? p - target : now, moves = Math.abs(now - aft) > 0.05;
    if (moves) sc.insertAdjacentHTML("beforeend", `<span class="bx-path" style="left:${pct(Math.min(now, aft))};right:calc(100% - ${pct(Math.max(now, aft))})"></span><span class="bx-aft" style="left:${pct(aft)}"></span>`);
    sc.insertAdjacentHTML("beforeend", `<span class="bx-now ${cls(now, B)}" style="left:${pct(now)}"></span>`);
    const b = el("b", {class: cls(now, B)}, word(now, B)); txt.append(b);
    if (moves && word(now, B) !== word(aft, B)) txt.append(" ", el("span", {}, "→ " + word(aft, B)));
    row.setAttribute("data-tip", t("tip.balancecell", {now: fx(m), target: fx(target), tol: fx(B), after: p == null ? "–" : fx(p)}));
   } else txt.textContent = "–";
   wrap.append(row);
  }
 }
 box.append(wrap);
}

async function histOf(stint) { const k = `${stint}|${S.hist.kerbs}`; if (!S.hists[k]) S.hists[k] = api(`/api/histograms?stint=${stint}&kerbs=${S.hist.kerbs}&bin=10`).catch(() => null); return S.hists[k]; }

async function dampers(box) {
 const grid = el("div", {class: "hgrid"}); box.append(grid);
 const st = baseStint(), prev = st ? S.stints[S.stints.indexOf(st) - 1] : null;
 const [cur, before] = await Promise.all([histOf(st.id), prev ? histOf(prev.id) : null]);
 if (!grid.isConnected) return;
 if (!cur) { grid.append(el("div", {class: "empty"}, t("dampers.none"))); return; }
 const draw = () => { grid.replaceChildren(); for (const w of ["fl", "fr", "rl", "rr"]) { const cell = el("div", {class: "hw"}); grid.append(cell); histogram(cell, w, cur.wheels[w], before && before.wheels[w], cur.knee); } };
 draw();
 // charts are drawn at their pixel width, so redraw when the column width changes
 const size = () => `${grid.clientWidth}x${grid.clientHeight}`;
 let last = size();
 const ro = new ResizeObserver(() => { if (!grid.isConnected) { ro.disconnect(); return; } if (size() === last) return; last = size(); draw(); });
 ro.observe(grid);
}

function histogram(box, w, h, prev, knee) {
 const s = h.stats || {}, pct = v => v == null ? "–" : `${Math.round(v * 100)} %`;
 const top = el("div", {class: "top"}); top.append(el("b", {}, t("wheel." + w)), el("span", {}, t("dampers.stats", {bump: pct(s.bump), slow: pct(s.slow)})));
 box.append(top);
 const total = h.counts.reduce((a, b) => a + b, 0);
 if (!total) { box.append(el("div", {class: "empty"}, t("dampers.nodata"))); return; }
 const centres = h.edges.slice(0, -1).map((e, i) => (e + h.edges[i + 1]) / 2), width = h.edges[1] - h.edges[0], lim = 200;
 const hc = el("div", {class: "hc"}); box.append(hc);
 const W = Math.max(240, Math.round(hc.clientWidth) || 440), H = Math.max(50, Math.round(hc.clientHeight) || 120), m = {l: 4, r: 4, t: 4, b: 18}, sx = v => m.l + (v + lim) / (2 * lim) * (W - m.l - m.r);
 const dens = c => c.map(n => n / total / width), d = dens(h.counts);
 const ptot = prev ? prev.counts.reduce((a, b) => a + b, 0) : 0, pd = ptot ? dens(prev.counts.map(n => n * total / ptot)) : null;
 const topv = Math.max(...d.filter((_, i) => Math.abs(centres[i]) <= lim), ...(pd || [0])) * 1.1 || 1, sy = v => m.t + (1 - v / topv) * (H - m.t - m.b);
 const svg = svgBox(hc, W, H);
 svg.append(sv("rect", {x: sx(-knee), y: m.t, width: sx(knee) - sx(-knee), height: H - m.t - m.b, fill: css("--l3"), "fill-opacity": .6, rx: 4}));
 svg.append(sv("line", {x1: sx(0), x2: sx(0), y1: m.t, y2: H - m.b, stroke: css("--t3"), "stroke-opacity": .5}));
 const path = (vals, color, wd) => { let s2 = ""; centres.forEach((c, i) => { if (Math.abs(c) > lim) return; const y = sy(vals[i]); s2 += (s2 ? "L" : "M") + `${sx(c - width / 2)},${y}L${sx(c + width / 2)},${y}`; }); svg.append(sv("path", {d: s2, fill: "none", stroke: color, "stroke-width": wd})); };
 if (pd) path(pd, css("--t3"), 1.4);
 path(d, css("--t1"), 1.8);
 svg.append(sv("text", {x: m.l, y: H - 3}, t("dampers.rebound", {v: lim})), sv("text", {x: sx(0), y: H - 3, "text-anchor": "middle"}, "0"), sv("text", {x: W - m.r, y: H - 3, "text-anchor": "end"}, t("dampers.bump", {v: lim})));
}

export function renderSetup() {
 const tiles = $("tiles"), panel = $("panel"), pr = S.proposal, plan = pr && pr.plan;
 if (!S.session || !baseStint()) { tiles.replaceChildren(el("div", {class: "card wide empty"}, t("stints.none"))); panel.replaceChildren(); return; }
 const tile = (key, extra, wide) => { const c = el("div", {class: "card" + (wide ? " wide" : "")}), h = el("div", {class: "th"}); h.append(el("h3", {}, t(key + ".title"))); if (extra) h.append(extra); c.append(h); tiles.append(c); return c; };
 tiles.replaceChildren();
 const x = k => el("span", {class: "x"}, t(k));
 const bal = tile("balance", x("balance.sub"), true); balance(bal, plan);
 const car = CARS[family(pr && pr.car_class)];
 const side = tile("side", x("side.sub")), sb = el("div", {class: "carv"}); side.append(sb); drawSide(sb, plan && plan.platform, car);
 const top = tile("changes", x("changes.sub")), tb = el("div", {class: "carv"}); top.append(tb); drawTop(tb, pr ? pr.changes : [], car);
 const polys = tb.querySelectorAll("polygon"), texts = tb.querySelectorAll("text.v");
 (pr ? pr.changes : []).forEach((c, i) => { if (polys[i]) polys[i].dataset.k = i; if (texts[i]) texts[i].dataset.k = i; });
 const legend = el("div", {class: "legend"});
 legend.innerHTML = `<span><i style="background:var(--t1)"></i>${t("dampers.this")}</span><span><i style="background:var(--t3)"></i>${t("dampers.prev")}</span><span><i style="background:var(--l3)"></i>${t("dampers.slow")}</span>`;
 const kerbs = pills(null, ["off", "on", "both"], [t("kerbs.off"), t("kerbs.on"), t("kerbs.both")], S.hist.kerbs, v => { S.hist.kerbs = v; renderSetup(); });
 legend.append(el("span", {class: "kq"}, t("kerbs.label")), kerbs);
 const dmp = tile("dampers", legend, true); dampers(dmp);
 renderPanel(panel, pr, plan);
 requestAnimationFrame(refreshThumbs);
}

const LOCK = '<svg class="ico" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';
const UNLOCK = '<svg class="ico" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/></svg>';
const FLOPPY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3.5h11.5l3.5 3.5v11.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2z"/><path class="sh2" d="M7.5 3.5v5h7v-5"/><rect x="6.5" y="13" width="11" height="7.5" rx="1"/></svg>';

function renderPanel(p, pr, plan) {
 p.replaceChildren();
 const from = pr && stintById(pr.stint_id), q = plan && plan.short;
 p.append(el("h2", {}, t("next.title")));
 const n = pr && pr.changes ? pr.changes.length : 0;
 p.append(el("div", {class: "sub"}, pr ? t(n === 1 ? "next.sub1" : "next.sub", {n, stint: from ? stintNo(from) : "?"}) : t("setup.working")));
 if (q) {
  const st = stintById(q.stint), row = el("div", {class: "prow"});
  row.append(el("span", {}, t("next.short", {n: st ? stintNo(st) : "?"})), pills(null, [true, false], [t("next.undrivable"), t("next.fine")], q.undrivable, async v => {
   if (!st) return; try { st.rating = (await api("/api/rating", {file_name: st.file_name, undrivable: v})).rating; await loadProposal(); } catch (e) { setStatus(e.message, true); } }));
  p.append(row);
 }
 if (pr && !pr.changes.length && pr.why) { const w = el("div", {class: "why"}, pr.why.text); if (pr.why.facts && pr.why.facts.length) w.setAttribute("data-tip", pr.why.facts.join(" · ")); p.append(w); }
 const tg = S.feelTarget && plan && S.feelTarget.car_key === plan.car_key ? S.feelTarget : plan && plan.target;
 if (plan && tg) {
  for (const [axis, neg, pos] of [["front", "feel.pointier", "feel.lazier"], ["rear", "feel.looser", "feel.safer"], ["fast", "feel.looser", "feel.planted"]])
   p.append(el("div", {class: "plbl"}, t("feel." + axis)), feelSlider(axis, t(neg), t(pos), tg));
  const it = el("button", {type: "button", class: "ghost", "aria-pressed": tg.anchor === "fingerprint" && !tg.front && !tg.rear && !tg.fast, "data-tip": t("tip.thisisit")}, t("feel.thisisit"));
  it.onclick = () => feel({lock: true}); p.append(it);
 }
 if (plan && plan.gap && plan.gap.length) { const g = plan.gap[0]; p.append(el("div", {class: "gap"}, t(g.axis === "rear" ? "next.gap_rear" : "next.gap_front", {s: fx(g.seconds, 1)}))); }
 const bst = baseStint(), r = (bst && bst.rating) || {};
 if (pr && pr.brake_pressure_by_feel && bst) {
  const b = el("button", {type: "button", class: "ghost", "aria-pressed": !!r.lockups}, t("next.brakes"));
  b.onclick = async () => { try { bst.rating = (await api("/api/rating", {file_name: bst.file_name, lockups: !r.lockups})).rating; scheduleProposal(); renderSetup(); } catch (e) { setStatus(e.message, true); } };
  p.append(b);
 }
 const list = el("div", {class: "list" + (S.proposalBusy ? " busy" : "")});
 for (const [i, c] of (pr ? pr.changes : []).entries()) {
  const li = el("div", {class: "li"}), info = el("div");
  info.append(el("div", {class: "n"}, c.name), el("div", {class: "s"}, `${c.before_text} → ${c.approx ? "≈ " : ""}${c.after_text}`));
  if (c.reason) info.append(el("div", {class: "r"}, c.reason));
  const b = el("button", {type: "button", class: "lock", "aria-pressed": "false", "aria-label": t("lock.lock", {name: c.name}), "data-tip": t("tip.lock")}); b.innerHTML = UNLOCK;
  b.onclick = () => setLock(c.keys, true);
  li.append(info, b);
  li.onmouseenter = () => document.querySelectorAll(`#tiles [data-k="${i}"]`).forEach(e => e.classList.add("hot"));
  li.onmouseleave = () => document.querySelectorAll(`#tiles [data-k="${i}"]`).forEach(e => e.classList.remove("hot"));
  list.append(li);
 }
 for (const l of (pr && pr.locks) || []) {
  const li = el("div", {class: "li locked"}), b = el("button", {type: "button", class: "lock", "aria-pressed": "true", "aria-label": t("lock.unlock", {name: l.name}), "data-tip": t("tip.unlock")}); b.innerHTML = LOCK;
  b.onclick = () => setLock([l.key], false); li.append(el("div", {class: "n"}, l.name), b); list.append(li);
 }
 if (list.childElementCount) p.append(list);
 p.append(el("div", {class: "status", id: "status"}));
 const foot = el("div", {class: "foot"}), lap = plan && plan.lap, tt = el("span", {class: "t"});
 if (lap && lap.delta != null) { tt.textContent = `${signed(lap.delta, 2)} s`; tt.append(el("small", {}, t("next.predicted", {sd: fx(lap.sd, 2)}))); }
 const ready = !!(pr && pr.changes.length && pr.target && !pr.error), done = ready && S.written.has(pr.signature);
 const w = el("button", {type: "button", class: "fab", id: "write"}); w.innerHTML = FLOPPY; w.append(done ? t("next.written") : t("next.write"));
 w.disabled = !ready || done || S.proposalBusy;
 w.onclick = async () => { w.disabled = true; try { await api("/api/write", {stint: baseStint().id, signature: pr.signature}); S.written.add(pr.signature); S.status = t("next.writtenmsg"); } catch (e) { S.status = ""; setStatus(e.message, true); await loadProposal(); return; } renderSetup(); setStatus(S.status); };
 foot.append(tt, w); p.append(foot);
 if (pr && pr.error) setStatus(pr.error, true); else if (S.proposalBusy) setStatus(t("setup.working"));
}

async function setLock(keys, locked) { const st = baseStint(); if (!st) return; try { await api("/api/locks", {stint: st.id, keys, locked}); await loadProposal(); } catch (e) { setStatus(e.message, true); } }

// A feel slider: centred is "As it was", either end is the span (deg); the shaded middle
// is the dead zone, a move too small for the stint's measurement to act on.
function feelSlider(axis, neg, pos, tg) {
 const span = tg.span || 0.5, dz = Math.min(span, (tg.deadzone || {})[axis] || 0), v0 = tg[axis] || 0;
 const box = el("div", {class: "fs"}), ends = el("div", {class: "ends"}), val = el("span", {class: "v"});
 const inp = el("input", {type: "range", min: -span, max: span, step: 0.025, value: v0, "aria-label": `${neg} / ${pos}`});
 const dzp = dz / span * 50;
 inp.style.setProperty("--trk", `linear-gradient(to right, transparent calc(50% - 1px), var(--t3) calc(50% - 1px), var(--t3) calc(50% + 1px), transparent calc(50% + 1px)), linear-gradient(to right, var(--l3) ${50 - dzp}%, var(--sel) ${50 - dzp}%, var(--sel) ${50 + dzp}%, var(--l3) ${50 + dzp}%)`);
 const show = v => {
  const a = Math.abs(v);
  val.textContent = a < 1e-9 ? t("feel.asitwas") : `${v < 0 ? neg : pos} ${+a.toFixed(3)}°` + (a <= dz ? " · " + t("feel.small") : "");
  val.classList.toggle("small", a > 1e-9 && a <= dz);
 };
 show(v0);
 inp.oninput = () => show(+inp.value);
 inp.onchange = () => feel({axis, value: +inp.value});
 inp.ondblclick = () => { inp.value = 0; show(0); feel({axis, value: 0}); };
 ends.append(el("span", {}, neg), val, el("span", {}, pos));
 box.append(ends, inp);
 return box;
}

let feelBusy = false;
async function feel(body) {
 const st = baseStint(); if (!st || feelBusy) return; feelBusy = true;
 try { const r = await api(body.lock ? "/api/lock" : "/api/feedback", {stint: st.id, ...body}); S.feelTarget = r.target; await loadProposal(); S.feelTarget = null; if (S.screen === "setup") renderSetup(); }
 catch (e) { setStatus(e.message, true); } finally { feelBusy = false; }
}
