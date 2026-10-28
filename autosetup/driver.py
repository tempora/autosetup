"""The Driver screen: what kind of car you like and where you take setups, per car class.

* Rotation: the understeer angle you like per corner phase (opt/balance.py) against the
  usual one (the knowledge file's, before your ratings moved it).
* Setups you go towards: per car + track, the starting setup against the setups you kept
  (drove again or built on, without reverting) or rated 4–5, as a share of each
  setting's range; averaged over the car/tracks of the class.
* Trade-off: the learned weights (preference.py), shared across cars.
"""

from __future__ import annotations

import json
from collections import defaultdict

import numpy as np

from autosetup.advisor import load_knowledge
from autosetup.analysis import pretty_name
from autosetup.lmu.setup_space import CarSetup, parse_number
from autosetup.opt.balance import learn, rated_phases
from autosetup.opt.dataset import ZONES, build_dims
from autosetup.opt.preference import _distance, fingerprint

MIN_SHARE = 0.05  # moved less than this share of a range: not a tendency
MAX_SETTINGS = 8
WHERE = {f"{sp}_{ph}": f"{s} {p}" for sp, s in (("slow", "in low-speed"), ("medium", "in mid-speed"),
                                                  ("fast", "in high-speed"))
         for ph, p in (("entry", "corner entries"), ("mid", "mid-corner"), ("exit", "corner exits"))}
MORE_LESS = ("WING", "TRACTION", "ANTILOCK", "DIFF", "PACKERS", "BRAKE_DUCTS", "PRESSURE")


def profile(store, weights: dict) -> dict:
    classes = store.con.execute(
        """select car_class, max(file_mtime) from recordings where car_class is not null and duration > 30
           group by car_class order by 2 desc""").fetchall()
    out = []
    for (car_class, _) in classes:
        laps = store.con.execute(
            """select count(*) from laps l join recordings r on r.id = l.recording_id
               where r.car_class = ? and l.complete and not l.in_pits""", [car_class]).fetchone()[0]
        if not laps:
            continue
        ratings = store.con.execute(
            """select count(*) from ratings g join recordings r using (file_name) where r.car_class = ?
               and coalesce(g.rating, g.entry, g.mid, g."exit") is not null""", [car_class]).fetchone()[0]
        out.append({"car_class": car_class, "laps": int(laps), "ratings": int(ratings),
                    "rotation": _rotation(store, car_class), "setups": _setups(store, car_class)})
    return {"classes": out, "tradeoff": {k: weights.get(k) for k in ("pace", "drivability", "tyres")}}


def _rotation(store, car_class: str) -> list[dict]:
    rated = rated_phases(store, car_class)
    if not rated:
        return []
    k = load_knowledge(car_class)
    bal = learn(rated, k)
    rows = []
    for z in ZONES:
        t = bal.targets[z]
        usual = t.limit - t.per_point
        rows.append({"phase": z, "liked": t.angle, "usual": usual, "n": t.n,
                     "line": _rotation_line((t.angle - usual) / t.per_point, z)})
    return rows


def _rotation_line(points: float, zone: str) -> str:
    where = WHERE[zone]
    if abs(points) < 0.5:
        return f"About usual {where}"
    how = "Much " if abs(points) >= 1.5 else ("A bit " if abs(points) < 1 else "")
    if points < 0:
        return f"{how}pointier than usual {where}".capitalize()
    return f"{how}more understeer than usual {where}".capitalize()


def _setups(store, car_class: str) -> list[dict]:
    rows = store.con.execute(
        """select r.car_signature, coalesce(r.track_layout, r.track), r.file_name, r.setup_json, g.rating
           from recordings r left join ratings g using (file_name)
           where r.car_class = ? and r.duration > 30 and r.car_signature is not null
           order by r.file_mtime""", [car_class]).fetchall()
    by_ctx: dict[tuple, list] = defaultdict(list)
    for sig, layout, _, setup_json, rating in rows:
        by_ctx[(sig, layout)].append((CarSetup.from_json(json.loads(setup_json)), rating))

    drift: dict[str, list[float]] = defaultdict(list)  # dim: per car/track mean share moved
    label_dir: dict[str, list[int]] = defaultdict(list)  # dim: does a higher index raise the label's number?
    keys_of: dict[str, list[str]] = {}
    for stints in by_ctx.values():
        kept = _kept(stints)
        if not kept:
            continue
        start = stints[0][0]
        for d in build_dims(start):
            if not all(k in s.params for s in kept for k in d.keys):
                continue
            keys_of[d.name] = d.keys
            drift[d.name].append(float(np.mean([d.x(s) - d.x(start) for s in kept])))
            for s in kept:
                a, b = start[d.keys[0]], s[d.keys[0]]
                na, nb = parse_number(a.text), parse_number(b.text)
                if a.value != b.value and na is not None and nb is not None and na != nb:
                    label_dir[d.name].append(1 if (b.value - a.value) * (nb - na) > 0 else -1)
    out = []
    for name, vals in drift.items():
        share = float(np.mean(vals))
        if abs(share) < MIN_SHARE:
            continue
        up = share > 0
        out.append({"name": _name(keys_of[name]), "delta": share, "tracks": len(vals),
                    "line": _setup_line(keys_of[name], up, label_dir.get(name, []))})
    out.sort(key=lambda r: -abs(r["delta"]))
    return out[:MAX_SETTINGS]


def _kept(stints: list[tuple[CarSetup, int | None]]) -> list[CarSetup]:
    """Setups after the first that you stuck with: driven in two stints or more, or built
    on by the next one, and not reverted; or rated 4–5."""
    runs: list[dict] = []
    for setup, rating in stints:
        fp = fingerprint(setup)
        if runs and runs[-1]["fp"] == fp:
            runs[-1]["n"] += 1
            runs[-1]["best"] = max(runs[-1]["best"], rating or 0)
        else:
            runs.append({"fp": fp, "setup": setup, "n": 1, "best": rating or 0})
    kept = []
    for i in range(1, len(runs)):
        run, nxt = runs[i], runs[i + 1] if i + 1 < len(runs) else None
        reverted = nxt is not None and any(runs[j]["fp"] == nxt["fp"] for j in range(i))
        built_on = nxt is not None and not reverted and _distance(nxt["fp"], run["fp"]) < _distance(nxt["fp"], runs[i - 1]["fp"])
        if run["best"] >= 4 or (not reverted and (run["n"] >= 2 or built_on)):
            kept.append(run["setup"])
    return kept


def _name(keys: list[str]) -> str:
    if len(keys) == 1:
        return pretty_name(keys[0])
    base = pretty_name(keys[0]).rsplit(" ", 1)[0]
    return f"{'Front' if keys[0].endswith(('FL', 'FR')) else 'Rear'} {base.lower()}"


def _setup_line(keys: list[str], up: bool, label_dir: list[int]) -> str:
    """'Softer front anti-roll bar', 'Less rear wing', 'Brake bias rearward'."""
    key = keys[0]
    name = _name(keys)
    if key == "VM_BRAKE_BALANCE":  # its label reads front %
        number_up = up if not label_dir or sum(label_dir) >= 0 else not up
        return f"Brake bias {'forward' if number_up else 'rearward'}"
    lower = name if name[1:2].isupper() else name[0].lower() + name[1:]  # 'TC map' stays
    if any(w in key for w in MORE_LESS):
        return f"{'More' if up else 'Less'} {lower}"
    return f"{'Stiffer' if up else 'Softer'} {lower}"
