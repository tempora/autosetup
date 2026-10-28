""""This is it": the balance you lock for a car, and what it becomes elsewhere.

Locking freezes the balance measured on the stint you're on (pooled with the stints
before it on the same setup) as the car's fingerprint: a target per phase × speed band
with a tolerance band (no more rotation, no less). It keeps the aero platform it was
driven on (wing, ride heights static and at speed, rake, aero balance as high- minus
low-speed understeer), the speed-band split of that track, and the setup itself.

A fingerprint belongs to the car (any track, and it outlives game updates: it's about
you). On a track where the car hasn't been driven it's the target as it stands; for a
car of the class without one of its own, the class's fingerprints are pooled into one,
with a wider band (how a class feels carries over; not exactly).

Each fingerprint is also a point on two axes against the class's usual balance (the
median of every stint of the class): Looser ↔ Safer from mid and exit, Pointier ↔ Lazier
from entry, in button steps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from autosetup.feel.cars import recording_car
from autosetup.feel.measure import CELLS, Measured, cell_parts, measured, same_setup_stints
from autosetup.feel.target import SCHEMA as EVENTS_SCHEMA
from autosetup.feel.target import tolerance_of
from autosetup.lmu.builds import current_build
from autosetup.lmu.setup_space import CarSetup
from autosetup.opt.space import main_dims

SCHEMA = """
create sequence if not exists fingerprint_id;
create table if not exists fingerprints (
    id            integer primary key default nextval('fingerprint_id'),
    car_key       varchar,
    car_class     varchar,
    layout        varchar,
    recording_id  integer,
    stints        json,
    created_at    timestamp,
    game_build    varchar,
    measured      json,                 -- feel/measure.py Measured
    tolerance     json,                 -- ± deg per cell
    platform      json,                 -- wing, ride heights, rake, aero balance
    setup         json,                 -- main settings: name → index, and the labels
    active        boolean
);
"""
CLASS_WIDEN = 1.5


@dataclass
class Fingerprint:
    id: int | None
    car_key: str
    car_class: str
    layout: str
    recording_id: int
    cells: dict[str, float]
    tolerance: dict[str, float]
    measured: Measured
    platform: dict
    setup: dict
    created_at: str = ""
    transferred: str | None = None  # None (the car's own) | "class"
    sources: list[str] = field(default_factory=list)  # cars a class fingerprint came from

    @property
    def turnin(self) -> float | None:
        return self.measured.turnin

    @property
    def countersteer(self) -> float | None:
        return self.measured.countersteer

    def to_json(self) -> dict:
        return {"id": self.id, "car_key": self.car_key, "car_class": self.car_class, "layout": self.layout,
                "stint": self.recording_id, "cells": self.cells, "tolerance": self.tolerance,
                "turnin": self.turnin, "countersteer": self.countersteer, "mix": self.measured.mix,
                "platform": self.platform, "setup": self.setup, "created_at": self.created_at,
                "transferred": self.transferred, "sources": self.sources}


def _schema(store) -> None:
    store.con.execute(EVENTS_SCHEMA)
    store.con.execute(SCHEMA)


def _num(text: str) -> float | None:
    from autosetup.lmu.setup_space import parse_number

    return parse_number(text)


def platform_of(store, rid: int, setup: CarSetup) -> dict:
    from autosetup.sim.model import stint_balance, wing_share

    rhf, rhr = store.con.execute("select rh_front_fast, rh_rear_fast from recordings where id = ?", [rid]).fetchone()

    def static(keys):
        vals = [_num(setup[k].text) for k in keys if k in setup.params]
        vals = [v for v in vals if v is not None]
        return float(np.mean(vals)) * 10.0 if vals else None  # labels are cm

    wing = setup.params.get("VM_REAR_WING")
    bal = stint_balance(store, rid)
    f, r = static(["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"]), static(["WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"])
    return {"wing": wing.text.strip() if wing else None, "wing_share": wing_share(setup),
            "rh_front": f, "rh_rear": r, "rh_front_fast": rhf, "rh_rear_fast": rhr,
            "rake": (r - f) if f is not None and r is not None else None,
            "rake_fast": (rhr - rhf) if rhf is not None and rhr is not None else None,
            "aero_balance": bal[0] if bal else None}


def lock(store, rid: int, knowledge: dict) -> Fingerprint:
    """Make the balance of this stint (and the stints before it on the same setup) the
    car's fingerprint: what you drove, as it was, so the stint's toggles go."""
    _schema(store)
    car_key, car_class = recording_car(store, rid)
    stints = same_setup_stints(store, rid)
    m = measured(store, stints)
    if not m.cells:
        raise ValueError("No corner passes on this stint to lock")
    setup = store.setup_of(rid)
    layout = store.con.execute("select coalesce(track_layout, track) from recordings where id = ?", [rid]).fetchone()[0]
    tol = tolerance_of(m, knowledge)
    settings = {d.name: {"index": d.index(setup), "text": setup[d.keys[0]].text.strip(), "keys": list(d.keys)}
                for d in main_dims(setup)}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    store.con.execute("update fingerprints set active = false where car_key = ?", [car_key])
    fid = store.con.execute(
        """insert into fingerprints (car_key, car_class, layout, recording_id, stints, created_at, game_build,
               measured, tolerance, platform, setup, active)
           values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, true) returning id""",
        [car_key, car_class, layout, rid, json.dumps(stints), now, current_build(store), json.dumps(m.to_json()),
         json.dumps(tol), json.dumps(platform_of(store, rid, setup)), json.dumps(settings)]).fetchone()[0]
    store.con.execute("insert into feel_events (car_key, car_class, kind, recording_id, fingerprint, happened_at) "
                      "values (?, ?, 'lock', ?, ?, ?)", [car_key, car_class, rid, fid, now])
    store.con.execute("delete from feel_toggles where recording_id = ?", [rid])
    return own(store, car_key)


def forget(store, car_key: str) -> int:
    """Forget a car's fingerprint: the target goes back to what
    you're driving. Returns how many were active."""
    _schema(store)
    n = store.con.execute("select count(*) from fingerprints where car_key = ? and active", [car_key]).fetchone()[0]
    store.con.execute("update fingerprints set active = false where car_key = ?", [car_key])
    car_class = store.con.execute("select any_value(car_class) from fingerprints where car_key = ?", [car_key]).fetchone()[0]
    store.con.execute("insert into feel_events (car_key, car_class, kind, happened_at) values (?, ?, 'reset', ?)",
                      [car_key, car_class, datetime.now(timezone.utc).replace(tzinfo=None)])
    return int(n)


def active(store) -> list[Fingerprint]:
    return _row(store, "active", [])


def _row(store, where: str, params: list) -> list[Fingerprint]:
    _schema(store)
    cur = store.con.execute(f"select * from fingerprints where {where} order by id", params)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        m = Measured.from_json(json.loads(d["measured"]))
        out.append(Fingerprint(d["id"], d["car_key"], d["car_class"], d["layout"], d["recording_id"], m.mean(),
                               json.loads(d["tolerance"]), m, json.loads(d["platform"]), json.loads(d["setup"]),
                               str(d["created_at"])))
    return out


def own(store, car_key: str) -> Fingerprint | None:
    rows = _row(store, "car_key = ? and active", [car_key])
    return rows[-1] if rows else None


def of_class(store, car_class: str) -> list[Fingerprint]:
    return _row(store, "car_class = ? and active", [car_class])


def class_fingerprint(store, car_class: str, exclude: str | None = None) -> Fingerprint | None:
    """The class's fingerprints (other cars) pooled: per cell the mean, the band widened."""
    fps = [f for f in of_class(store, car_class) if f.car_key != exclude]
    if not fps:
        return None
    cells, tol = {}, {}
    for c in CELLS:
        have = [f for f in fps if c in f.cells]
        if not have:
            continue
        w = np.array([1 / f.tolerance[c] ** 2 for f in have])
        cells[c] = float(np.sum(w * np.array([f.cells[c] for f in have])) / w.sum())
        spread = float(np.std([f.cells[c] for f in have]))
        tol[c] = CLASS_WIDEN * max(float(np.mean([f.tolerance[c] for f in have])), spread)
    newest = fps[-1]
    return Fingerprint(None, newest.car_key, car_class, newest.layout, newest.recording_id, cells, tol,
                       newest.measured, newest.platform, {}, newest.created_at, "class", [f.car_key for f in fps])


def anchor_for(store, car_key: str, car_class: str, knowledge: dict) -> Fingerprint | None:
    """What a car's target starts from: its own fingerprint, else its class's."""
    return own(store, car_key) or class_fingerprint(store, car_class, exclude=car_key)


# -- the driver profile -----------------------------------------------------------------
def usual(store, car_class: str) -> dict[str, float]:
    """The class's usual balance per cell: the median over every stint of the class."""
    from autosetup.lmu.builds import current_filter

    cond, params = current_filter(store)
    rids = [r for (r,) in store.con.execute(
        f"select id from recordings r where car_class = ? and {cond}", [car_class, *params]).fetchall()]
    per_cell: dict[str, list[float]] = {}
    for rid in rids:
        for c, v in measured(store, [rid]).cells.items():
            per_cell.setdefault(c, []).append(v.mean)
    return {c: float(np.median(v)) for c, v in per_cell.items()}


def axes(fp: Fingerprint, baseline: dict[str, float], step: float) -> tuple[float, float]:
    """(rear: − looser … + safer, front: − pointier … + lazier), in button steps from the
    class's usual balance."""
    def axis(phases: dict[str, float]) -> float:
        num = den = 0.0
        for c, v in fp.cells.items():
            ph, _ = cell_parts(c)
            w = phases.get(ph, 0.0)
            if w and c in baseline:
                num += w * (v - baseline[c])
                den += w
        return num / den / step if den else 0.0

    return axis({"mid": 0.7, "exit": 1.0}), axis({"entry": 1.0})


def summary(fp: Fingerprint, baseline: dict[str, float], step: float, usual_rake: float | None) -> str:
    """One line: 'Pointy entry, stable exit, slightly more rake than usual'."""
    rear, front = axes(fp, baseline, step)

    def words(x: float, neg: str, pos: str, mid: str) -> str:
        if abs(x) < 0.5:
            return mid
        how = "very " if abs(x) >= 2 else ("" if abs(x) >= 1 else "slightly ")
        return how + (neg if x < 0 else pos)

    parts = [f"{words(front, 'pointy', 'lazy', 'neutral')} entry", f"{words(rear, 'loose', 'stable', 'neutral')} exit"]
    rake = fp.platform.get("rake_fast")
    if rake is not None and usual_rake is not None and abs(rake - usual_rake) >= 2.0:
        d = rake - usual_rake
        parts.append(f"{'slightly ' if abs(d) < 5 else ''}{'more' if d > 0 else 'less'} rake than usual")
    line = ", ".join(parts)
    return line[0].upper() + line[1:]
