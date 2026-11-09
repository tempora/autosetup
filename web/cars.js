import {sv, svgBox, css, fx, signed, t} from "./state.js";

function spline(pts, closed = true) {
 const f = v => +v.toFixed(4);
 const d = (a, b) => Math.max(1e-9, Math.sqrt(Math.hypot(b[0] - a[0], b[1] - a[1])));
 const curve = (q, loop) => {  // the C segments through q (no M)
  const n = q.length, P = i => loop ? q[(i + n) % n] : q[Math.max(0, Math.min(n - 1, i))];
  let out = "";
  for (let i = 0; i < (loop ? n : n - 1); i++) {
   const p0 = P(i - 1), p1 = P(i), p2 = P(i + 1), p3 = P(i + 2);
   const d1 = d(p0, p1), d2 = d(p1, p2), d3 = d(p2, p3);
   const c1 = [0, 1].map(k => (d1 * d1 * p2[k] - d2 * d2 * p0[k] + (2 * d1 * d1 + 3 * d1 * d2 + d2 * d2) * p1[k]) / (3 * d1 * (d1 + d2)));
   const c2 = [0, 1].map(k => (d3 * d3 * p1[k] - d2 * d2 * p3[k] + (2 * d3 * d3 + 3 * d3 * d2 + d2 * d2) * p2[k]) / (3 * d3 * (d3 + d2)));
   out += ` C${f(c1[0])},${f(c1[1])} ${f(c2[0])},${f(c2[1])} ${f(p2[0])},${f(p2[1])}`;
  }
  return out;
 };
 // A point [x, y, 1] is a corner: the curve stops there and starts again.
 const k = pts.findIndex(p => p[2]);
 if (k < 0) return `M${f(pts[0][0])},${f(pts[0][1])}` + curve(pts, closed) + (closed ? " Z" : "");
 const q = closed ? [...pts.slice(k), ...pts.slice(0, k), pts[k]] : pts;
 let out = `M${f(q[0][0])},${f(q[0][1])}`, start = 0;
 for (let i = 1; i < q.length; i++) if (q[i][2] || i === q.length - 1) { out += curve(q.slice(start, i + 1), false); start = i; }
 return out + (closed ? " Z" : "");
}

// Points around a wheel arch, rear lip to front lip (the outline runs tail to nose along the floor):
// a crisp lip at the sill, then round over the wheel.
const wheelArch = (c, h, r, floor) => [[c + r + 0.006, floor, 1], [c + r, h - 0.02],
 ...[0, 30, 60, 90, 120, 150, 180].map(a => [c + r * Math.cos(a * Math.PI / 180), h + r * Math.sin(a * Math.PI / 180)]),
 [c - r, h - 0.02], [c - r - 0.006, floor, 1]];
const arch = (c, h, r, floor) => { const q = 0.552 * r;
 return `L${c + r},${floor} L${c + r},${h} C${c + r},${h + q} ${c + q},${h + r} ${c},${h + r} C${c - q},${h + r} ${c - r},${h + q} ${c - r},${h} L${c - r},${floor}`; };
// Top-view helpers in the car's frame (u across, v along): both sides of a shape, an ellipse.
const mirrored = d => [d, d.replace(/(-?[\d.]+),(-?[\d.]+)/g, (_, u, v) => `${-u},${v}`)];
const ellipse = (cu, cv, ru, rv) => { const a = 0.552 * ru, b = 0.552 * rv;
 return `M${cu - ru},${cv} C${cu - ru},${cv - b} ${cu - a},${cv - rv} ${cu},${cv - rv} C${cu + a},${cv - rv} ${cu + ru},${cv - b} ${cu + ru},${cv} ` +
  `C${cu + ru},${cv + b} ${cu + a},${cv + rv} ${cu},${cv + rv} C${cu - a},${cv + rv} ${cu - ru},${cv + b} ${cu - ru},${cv} Z`; };

export const CARS = {
 proto: {  // Hypercar, LMP2, LMP3: 5.1 m long, 3.15 m wheelbase
  axles: [0.2, 0.81], r: 0.07,
  side: "M0,.012 L.004,.03 C.03,.06 .09,.1 .13,.135 C.15,.165 .25,.178 .29,.148 L.36,.142 C.42,.15 .45,.2 .52,.206 " +
   "C.58,.21 .62,.2 .66,.186 L.75,.17 C.78,.185 .85,.186 .88,.174 L.98,.155 L1,.13 L1,.03 L.95,.012 " +
   arch(.81, .07, .086, .012) + " " + arch(.2, .07, .086, .012) + " Z",
  sideExtra: ["M.6,.205 C.7,.21 .8,.214 .87,.212", "M.86,.218 L1.005,.226 L1.005,.208 L.87,.203 Z", "M.94,.205 L.94,.178",
   "M.46,.18 C.5,.194 .58,.196 .63,.184"],
  half: [[.05, 0], [.13, .01], [.17, .04], [.195, .1], [.195, .3], [.15, .36], [.14, .55], [.17, .64], [.195, .7], [.195, .93], [.17, .98], [.1, .995]],
  tops: [[[0, .3], [.045, .33], [.06, .4], [.055, .5], [.035, .6], [0, .66], [-.035, .6], [-.055, .5], [-.06, .4], [-.045, .33]],
   [[-.2, .93], [.2, .93], [.2, .975], [-.2, .975]]],
  wheel: {u: .15, w: .07, l: .14, hidden: true}, wing: .955,
 },
 lmp2: {  // traced from an Oreca 07 (LMP2, ELMS LMP2) side view: x 0 at the splitter, 1 at the diffuser
  axles: [0.211, 0.844], r: 0.07,
  side: spline([[0, .008, 1], [.006, .025], [.015, .045], [.024, .065], [.04, .085], [.058, .1], [.064, .118],  // splitter up the nose
   [.074, .133], [.084, .137], [.1, .141], [.119, .1476], [.135, .1556], [.153, .1614], [.18, .1644], [.2, .1649],  // nose, fender
   [.233, .1644], [.259, .1636], [.28, .1622], [.299, .1702], [.312, .1795], [.3385, .1928], [.365, .2021], [.3915, .2106],  // canopy
   [.402, .214, 1], [.403, .232, 1], [.455, .235, 1], [.458, .242, 1],
   [.515, .242, 1], [.54, .236], [.56, .234, 1], [.938, .2363, 1], [.938, .23, 1], [.943, .229, 1], [1.008, .229, 1],  // fin, end plate
   [1.0056, .143, 1], [1, .141, 1],
   [1, .018, 1], ...wheelArch(.844, .07, .078, .016), ...wheelArch(.211, .07, .078, .016), [.05, .01], [.02, .005, 1]]),
  sideExtra: [
   spline([[.43, .197, 1], [.52, .197], [.533, .19, 1], [.495, .148, 1], [.426, .148, 1], [.42, .16]]),  // cockpit window
   spline([[.0736, .133, 1], [.0961, .1293, 1], [.0816, .1032, 1], [.067, .1072, 1]]),  // headlight
   spline([[.272, .149, 1], [.323, .186, 1], [.37, .1875, 1], [.349, .162], [.336, .157, 1]]),  // windscreen
   spline([[.5297, .2363], [.5782, .2213], [.6165, .205], [.6547, .1875], [.693, .17], [.7313, .1588], [.75, .155]], false),  // fin base
   spline([[.8869, .2363], [.8716, .1575]], false),  // fin trailing edge
   spline([[.943, .229, 1], [.943, .143, 1], [1.0056, .143, 1]], false),  // wing: one box at the end
  ],
  sideDetail: [
   spline([[.562, .098], [.592, .136], [.633, .151], [.722, .153], [.811, .153], [.882, .145], [.923, .136], [.97, .127]], false),  // haunch
   spline([[.391, .207, 1], [.391, .118, 1], [.544, .118, 1], [.544, .201, 1]], false),  // door
   spline([[.036, .081], [.075, .094], [.124, .113]], false), spline([[.012, .04], [.06, .052], [.1, .067]], false),  // dive planes
   spline([[.3, .1], [.4, .098], [.5, .085], [.56, .06], [.6, .045], [.75, .04]], false),  // sidepod
  ],
  half: [[.05, 0], [.12, .005], [.17, .03], [.195, .08], [.2, .15], [.2, .3], [.194, .36], [.19, .5], [.193, .62],
   [.2, .72], [.2, .9], [.19, .96], [.17, .995], [.05, 1]],  // sidepods nearly as wide as the wheels: no hourglass
  topDetail: [
   spline([[0, .36], [.05, .39], [.065, .45], [.06, .52], [.035, .58], [0, .6], [-.035, .58], [-.06, .52], [-.065, .45], [-.05, .39]]),  // canopy
   "M0,.6 L0,.94",  // fin
   ...mirrored("M.1,.02 C.125,.12 .14,.22 .13,.31"), ...mirrored(ellipse(.15, .04, .022, .028)),  // fender pods, headlights
   ...mirrored("M.15,.6 C.16,.68 .165,.74 .168,.8"),  // haunches
   "M-.2,.945 L.2,.945 L.2,.995 L-.2,.995 Z", ...mirrored("M.2,.935 L.2,1.005"),  // wing, end plates
  ],
  wheel: {u: .15, w: .07, l: .14, hidden: true}, wing: .97, smooth: true,
 },
 gt: {  // traced from a Porsche 911 GT3 R (992) side view: x 0 at the splitter, 1 at the rear bumper
  axles: [0.224, 0.775], r: 0.074,
  side: spline([[0, .028, 1], [.004, .05], [.01, .085], [.022, .106], [.045, .122], [.075, .14], [.1, .153], [.135, .161], [.17, .166],
   [.21, .17], [.26, .174], [.3, .184], [.35, .21], [.4, .24], [.453, .263], [.5, .271], [.55, .273], [.6, .272], [.66, .265],
   [.72, .247], [.78, .227], [.84, .206], [.88, .194], [.926, .171], [.965, .161], [.988, .15], [.996, .12], [.998, .08], [.997, .048],
   [.99, .025, 1], ...wheelArch(.775, .074, .08, .022), ...wheelArch(.224, .074, .082, .02), [.015, .02, 1]]),
  sideExtra: [
   spline([[.392, .181, 1], [.447, .229], [.456, .2365], [.466, .2425], [.478, .2462], [.492, .2476], [.55, .2479],
    [.616, .2468, 1], [.603, .188, 1]]),  // door window: straight up the pillar, then one even bend onto the top
   spline([[.626, .248, 1], [.66, .243], [.7, .232], [.73, .221], [.748, .212, 1], [.735, .202], [.72, .196], [.66, .191], [.61, .189, 1]]),  // rear quarter window
   spline([[.09, .127], [.1, .141], [.122, .155], [.146, .16], [.15, .153], [.14, .137], [.118, .125], [.098, .122]]),  // headlight
   spline([[.905, .158], [.945, .153], [.985, .146]], false),  // taillight
   spline([[.873, .198], [.882, .24], [.902, .281]], false), spline([[.89, .186], [.9, .232], [.92, .278]], false),  // swan necks
   spline([[.902, .281], [.925, .279], [.944, .275]], false),
   spline([[.944, .274], [.99, .276], [1.03, .276], [1.036, .268], [1.033, .245], [1.022, .234], [.99, .229], [.96, .225], [.95, .232], [.946, .255]]),  // wing end plate
  ],
  sideDetail: [
   spline([[.392, .181], [.41, .195], [.44, .196], [.441, .182], [.42, .176], [.398, .176]]),  // mirror
   spline([[.306, .15, 1], [.322, .152, 1], [.333, .108, 1], [.316, .106, 1]]),  // fender vent
   spline([[.627, .164, 1], [.655, .167], [.68, .162], [.676, .14], [.66, .12], [.63, .12, 1]]),  // rear intake
   spline([[.347, .177], [.343, .12], [.34, .07], [.338, .024]], false), spline([[.622, .183], [.618, .12], [.61, .07], [.601, .035]], false),  // door cut
   spline([[.554, .136], [.572, .137], [.589, .136]], false),  // door handle
  ],
  half: [[.06, 0], [.12, .004], [.16, .015], [.185, .04], [.196, .1], [.198, .2], [.196, .29], [.19, .36], [.188, .52], [.196, .6],
   [.206, .68], [.208, .78], [.207, .88], [.2, .94], [.175, .98], [.11, .996], [.04, 1]],  // hips only a little wider than the doors
  topDetail: [
   "M-.15,.34 C-.08,.325 .08,.325 .15,.34 C.148,.45 .142,.56 .135,.62 C.125,.68 .105,.72 .07,.74 C.03,.748 -.03,.748 -.07,.74 " +
    "C-.105,.72 -.125,.68 -.135,.62 C-.142,.56 -.148,.45 -.15,.34 Z",  // greenhouse
   "M-.145,.45 C-.08,.44 .08,.44 .145,.45", "M-.135,.62 C-.07,.612 .07,.612 .135,.62",  // windscreen | roof | rear window
   ...mirrored("M.11,.03 C.12,.14 .13,.25 .142,.335"),  // hood edges
   ...mirrored(ellipse(.145, .07, .03, .042)),  // headlights
   ...mirrored("M.188,.358 L.204,.361 M.202,.352 C.22,.349 .233,.355 .233,.363 C.229,.371 .215,.371 .202,.367 Z"),  // mirrors
   "M-.07,.765 L.07,.765", "M-.07,.79 L.07,.79", "M-.07,.815 L.07,.815",  // engine lid vents
   ...mirrored("M.065,.875 L.065,.95"),  // swan necks
   "M-.2,.95 C-.1,.94 .1,.94 .2,.95 L.2,1.03 C.1,1.022 -.1,1.022 -.2,1.03 Z",  // wing, over the tail
   ...mirrored("M.2,.944 L.2,1.036"),  // end plates
   ...mirrored("M.04,.988 C.1,.985 .15,.975 .178,.962"),  // light bar
  ],
  wheel: {u: .175, w: .06, l: .15, hidden: true}, wing: .99, smooth: true,
 },
};

// GT3, GTE, LMGT3 draw as the GT3 R; LMP2 (and ELMS LMP2) as the Oreca; anything else as a prototype.
export const family = cls => /GT/i.test(cls || "") ? "gt" : /LMP2/i.test(cls || "") ? "lmp2" : "proto";

// A path in the car's frame, each point mapped by f(x, y) → [px, py].
const carPath = (d, f) => d.replace(/(-?[\d.]+),(-?[\d.]+)/g, (_, x, y) => f(+x, +y).map(v => v.toFixed(1)).join(","));

export function drawSide(box, plat, car) {
 const W = 330, H = 190, L = 280, x0 = 25, ground = 150, k = 0.25;  // k: px per mm of ride height, exaggerated so rake shows
 const svg = svgBox(box, W, H);
 const cur = plat && plat.current, nxt = plat && plat.proposed;
 const [af, ar] = car.axles, ref = cur ? (cur.rh_front + cur.rh_rear) / 2 : 0;
 svg.append(sv("line", {x1: 10, x2: W - 10, y1: ground, y2: ground, stroke: css("--line"), "stroke-width": 1}));
 for (const a of car.axles) for (const [rr, w] of [[1, 2.5], [0.6, 1.5]])  // tyre and rim
  svg.append(sv("circle", {cx: x0 + a * L, cy: ground - car.r * L, r: car.r * L * rr, fill: "none", stroke: css("--muted"), "stroke-width": w}));
 const body = (p, dash) => {
  // The body pitched by the ride heights at the axles, straight through between and beyond.
  const lift = x => p ? ((p.rh_front + (p.rh_rear - p.rh_front) * (x - af) / (ar - af)) - ref) * k : 0;
  const f = (x, y) => [x0 + x * L, ground - y * L - lift(x)];
  const attrs = {fill: "none", stroke: dash ? css("--muted") : css("--text"), "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": dash ? "5 4" : "none"};
  svg.append(sv("path", {...attrs, "stroke-width": 2.5, d: carPath(car.side, f)}));
  for (const d of car.sideExtra) svg.append(sv("path", {...attrs, "stroke-width": 1.75, d: carPath(d, f)}));
  if (!dash) for (const d of car.sideDetail || []) svg.append(sv("path", {...attrs, stroke: css("--muted"), "stroke-width": 1.25, d: carPath(d, f)}));
 };
 // The proposed body only when it sits visibly apart; a pixel off just doubles every line.
 const moved = cur && nxt && Math.max(Math.abs(nxt.rh_front - cur.rh_front), Math.abs(nxt.rh_rear - cur.rh_rear)) * k >= 3;
 if (moved) body(nxt, true);
 body(cur, false);
 if (!cur) return;
 const num = (v, d, sign) => typeof v === "number" ? (sign ? signed(v, d) : fx(v, d)) : String(v ?? "–");
 const line = (a, b, d = 1, sign = false) => a === b || b == null ? num(a, d, sign) : `${num(a, d, sign)} → ${num(b, d, sign)}`;
 svg.append(sv("text", {x: x0 + af * L, y: ground + 18, "text-anchor": "middle", class: "v"}, t("side.rh", {v: line(cur.rh_front, nxt && nxt.rh_front)})));
 svg.append(sv("text", {x: x0 + ar * L, y: ground + 18, "text-anchor": "middle", class: "v"}, t("side.rh", {v: line(cur.rh_rear, nxt && nxt.rh_rear)})));
 svg.append(sv("text", {x: 10, y: 14}, t("side.rake", {v: line(cur.rake, nxt && nxt.rake, 1, true)})));
 if (cur.wing) svg.append(sv("text", {x: W - 10, y: 14, "text-anchor": "end"}, t("side.wing", {v: line(cur.wing, nxt && nxt.wing)})));
}

// Where a setting sits on the car seen from above: [side −1 left · 0 centre · 1 right, v].
function spot(key, car) {
 const [front, rear] = car.axles;
 const wheel = key.match(/-W_(FL|FR|RL|RR)$/);
 if (wheel) return {FL: [-1, front], FR: [1, front], RL: [-1, rear], RR: [1, rear]}[wheel[1]];
 if (key === "VM_REAR_WING") return [0, car.wing];
 if (key === "VM_FRONT_WING") return [0, 0.02];
 if (key === "VM_BRAKE_DUCTS") return [0, 0.07];
 if (/CASTER|STEER/.test(key)) return [0, front - 0.06];
 if (/^VM_(FRONT|LEFT|RIGHT)_/.test(key)) return [0, front];
 if (/^VM_REAR_/.test(key) || key === "VM_BRAKE_DUCTS_REAR") return [0, rear];
 return [0, 0.5];
}

export function drawTop(box, changes, car) {
 // From above, front on the left like the side view: the car's right side is at the top.
 const W = 330, H = 210, L = 260, x0 = 35, cy = 105;
 const svg = svgBox(box, W, H);
 const P = (u, v) => [x0 + v * L, cy - u * L];
 // Closed shapes through their points; smooth ones as a Catmull-Rom curve.
 const poly = (pts, stroke, width, smooth) => {
  const q = pts.map(([u, v]) => P(u, v)), n = q.length, f = v => v.toFixed(1);
  let d = `M${f(q[0][0])},${f(q[0][1])}`;
  for (let i = 0; i < n; i++) {
   const [a, b, c, e] = [q[(i - 1 + n) % n], q[i], q[(i + 1) % n], q[(i + 2) % n]];
   d += smooth ? `C${f(b[0] + (c[0] - a[0]) / 6)},${f(b[1] + (c[1] - a[1]) / 6)} ${f(c[0] - (e[0] - b[0]) / 6)},${f(c[1] - (e[1] - b[1]) / 6)} ${f(c[0])},${f(c[1])}`
    : `L${f(c[0])},${f(c[1])}`;
  }
  svg.append(sv("path", {d: d + "Z", fill: "none", stroke, "stroke-width": width, "stroke-linejoin": "round"}));
 };
 poly([...car.half, ...[...car.half].reverse().map(([u, v]) => [-u, v])], css("--text"), 2.5, car.smooth);
 for (const t2 of car.tops || []) poly(t2, css("--muted"), 1.75, false);
 for (const d of car.topDetail || []) svg.append(sv("path", {d: carPath(d, (u, v) => P(u, v)), fill: "none", stroke: css("--muted"),
  "stroke-width": 1.25, "stroke-linejoin": "round", "stroke-linecap": "round"}));
 const wh = car.wheel;
 for (const v of car.axles) for (const sgn of [-1, 1]) {
  const [x, y] = P(sgn * wh.u + wh.w / 2, v - wh.l / 2);
  svg.append(sv("rect", {x, y, width: wh.l * L, height: wh.w * L, fill: "none", stroke: css("--muted"), "stroke-width": 1.75,
   "stroke-dasharray": wh.hidden ? "3 2" : "none"}));  // a prototype's wheels sit under the bodywork
 }
 const half = Math.max(...car.half.map(h => h[0])) * L;
 if (!changes || !changes.length) { svg.append(sv("text", {x: x0 + L / 2, y: cy + half + 24, "text-anchor": "middle"}, t("changes.none"))); return; }
 // Labels above (right side and centre) and below (left side), each with a leader to its mark;
 // a label takes the row nearest the car where it doesn't overlap another.
 const taken = {up: [], down: []};
 const row = (where, x0, x1) => {
  let n = 0;
  while ((taken[where][n] || []).some(([a, b]) => x0 < b && a < x1)) n++;
  (taken[where][n] = taken[where][n] || []).push([x0, x1]);
  return n;
 };
 changes.forEach(c => {
  const [side, v] = spot(c.keys[0], car);
  const [x, y] = P(side * wh.u, v), up = side >= 0, a = 6, text = c.name;
  const tw = text.length * 6.6, cx = Math.min(W - tw / 2 - 2, Math.max(tw / 2 + 2, x)), n = row(up ? "up" : "down", cx - tw / 2 - 6, cx + tw / 2 + 6);
  svg.append(sv("polygon", {points: c.delta > 0 ? `${x},${y - a} ${x - a},${y + a} ${x + a},${y + a}` : `${x},${y + a} ${x - a},${y - a} ${x + a},${y - a}`, fill: css("--text")}));
  const ty = up ? cy - half - 10 - n * 14 : cy + half + 20 + n * 14, tx = cx;
  const txEl = sv("text", {x: tx, y: ty, "text-anchor": "middle", class: "v"}, text);
  txEl.style.fontSize = "11px";
  svg.append(sv("line", {x1: x, y1: up ? y - a - 2 : y + a + 2, x2: tx, y2: up ? ty + 3 : ty - 11, stroke: css("--line"), "stroke-width": 1}), txEl);
 });
}
