"""The balance the optimiser aims for, and the sliders that move it.

Three axes, a slider each, set on a stint, in degrees of understeer angle (− is more
rotation), anywhere within ± two of the class's ``feel_step_deg``:
  front  Pointier ↔ Lazier: turn-in (full) and mid-corner (a third)
  rear   Looser ↔ Safer: mid-corner (most) and exit (full)
  fast   Looser ↔ Planted: fast corners only, mid-corner most — the rear at speed

A slider asks for its move from how that stint felt: the target is the stint's balance
moved in the cells the axis covers, and the band there is kept under half the move, so
the setup you drove is outside it and something changes. A move smaller than what the
stint can resolve in any cell it covers (``deadzone``) changes nothing: the band can't
be narrower than the measurement. Each stint starts with the sliders centred (As it
was). With nothing moved the target is the car's locked fingerprint (fingerprint.py)
or, without one, what you drove. Sliders never move a setting themselves: the optimiser
finds settings that hit the target.

``feel_events`` keeps the locks and resets per car; ``feel_sliders`` the sliders per stint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from autosetup.feel.measure import Measured, cell_parts, measured, same_setup_stints

SCHEMA = """
create sequence if not exists feel_event_id;
create table if not exists feel_events (
    id            integer primary key default nextval('feel_event_id'),
    car_key       varchar,
    car_class     varchar,
    kind          varchar,              -- lock | reset
    axis          varchar,
    step          integer,
    recording_id  integer,              -- the stint you were on
    fingerprint   integer,              -- lock: the fingerprint it made
    happened_at   timestamp
);
create table if not exists feel_sliders (
    recording_id  integer primary key,  -- the stint the sliders are about
    car_key       varchar,
    car_class     varchar,
    front         double default 0,     -- deg: − pointier, + lazier
    rear          double default 0,     -- deg: − looser, + safer
    fast          double default 0,     -- deg: − looser, + planted (fast corners)
    set_at        timestamp
);
"""
AXES = ("front", "rear", "fast")
RANGE_STEPS = 2.0  # a slider's end, in feel_step_deg
# How much of an axis's move each phase gets (the cell's share of the slider).
AXIS_WEIGHT = {"front": {"braking": 0.0, "entry": 1.0, "mid": 0.3, "exit": 0.0},
               "rear": {"braking": 0.0, "entry": 0.0, "mid": 0.7, "exit": 1.0},
               "fast": {"braking": 0.0, "entry": 0.5, "mid": 1.0, "exit": 0.5}}  # high band only
RES_MIN = 0.05  # deg: no measurement resolves a balance finer than this
FAST_WEIGHT = 0.5  # with the fast slider moved, the fast cells count at least this much
TURNIN_PER_STEP = 0.03  # turn-in response (|r| / |r_ref|) one step of Pointier asks for more
COUNTERSTEER_PER_STEP = 0.5  # s per lap of countersteer one step of Looser accepts more


def axis_weight(axis: str, cell: str) -> float:
    ph, band = cell_parts(cell)
    if axis == "fast" and band != "high":
        return 0.0
    return AXIS_WEIGHT[axis][ph]


@dataclass
class Target:
    car_key: str
    car_class: str
    cells: dict[str, float]  # deg, + understeer
    tolerance: dict[str, float]  # ± deg around each cell that is "it"
    front: float = 0.0  # deg: − pointier, + lazier
    rear: float = 0.0  # deg: − looser, + safer
    fast: float = 0.0  # deg: − looser, + planted
    anchor: str = "none"  # fingerprint | moved | current | none
    anchor_stint: int | None = None
    fingerprint: int | None = None
    turnin: float | None = None
    countersteer: float | None = None  # s per lap accepted
    weights: dict[str, float] = field(default_factory=dict)  # per cell: how much it counts here (corner mix)
    deadzone: dict[str, float] = field(default_factory=dict)  # axis → deg a move must pass to change anything
    span: float = 0.5  # deg at a slider's end

    @property
    def moves(self) -> dict[str, float]:
        return {"front": self.front, "rear": self.rear, "fast": self.fast}

    @property
    def moved(self) -> bool:
        return any(abs(v) > 1e-9 for v in self.moves.values())

    def off(self, values: dict[str, float]) -> dict[str, float]:
        """Per cell, how far outside the band a balance is, in tolerances (0 inside)."""
        out = {}
        for c, v in values.items():
            if c in self.cells:
                out[c] = max(0.0, abs(v - self.cells[c]) - self.tolerance[c]) / self.tolerance[c]
        return out

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("car_key", "car_class", "cells", "tolerance", "front", "rear", "fast",
                                              "anchor", "anchor_stint", "fingerprint", "turnin", "countersteer",
                                              "weights", "deadzone", "span")}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def set_slider(store, car_key: str, car_class: str, recording_id: int, axis: str, value: float, span: float) -> None:
    """Set an axis's slider on a stint, in deg, clamped to ± ``span``."""
    store.con.execute(SCHEMA)
    if axis not in AXES:
        raise ValueError(f"axis must be one of {AXES}, not {axis!r}")
    v = max(-span, min(span, float(value)))
    cur = sliders(store, recording_id)
    cur[axis] = round(v, 4)
    store.con.execute("insert or replace into feel_sliders values (?, ?, ?, ?, ?, ?, ?)",
                      [recording_id, car_key, car_class, cur["front"], cur["rear"], cur["fast"], _now()])


def sliders(store, recording_id: int | None) -> dict[str, float]:
    """The sliders set on a stint; all centred when none are."""
    store.con.execute(SCHEMA)
    if recording_id is None:
        return {a: 0.0 for a in AXES}
    row = store.con.execute("select front, rear, fast from feel_sliders where recording_id = ?",
                            [recording_id]).fetchone()
    return {a: float(v or 0.0) for a, v in zip(AXES, row)} if row else {a: 0.0 for a in AXES}


def tolerance_of(m: Measured, knowledge: dict) -> dict[str, float]:
    """± deg per cell: at least ``feel_tolerance_deg``, wider where the measurement is noisy."""
    floor = float(knowledge["feel_tolerance_deg"])
    return {c: max(floor, 2.0 * v.se) for c, v in m.cells.items()}


def resolution_of(m: Measured) -> dict[str, float]:
    """Per cell, the smallest move the measurement resolves: its standard error, at least RES_MIN."""
    return {c: max(RES_MIN, v.se) for c, v in m.cells.items()}


def shifted(cells: dict[str, float], front: float = 0.0, rear: float = 0.0, fast: float = 0.0) -> dict[str, float]:
    moves = {"front": front, "rear": rear, "fast": fast}
    return {c: v + sum(m * axis_weight(a, c) for a, m in moves.items()) for c, v in cells.items()}


def narrowed(tolerance: dict[str, float], resolution: dict[str, float], front: float = 0.0, rear: float = 0.0,
             fast: float = 0.0) -> dict[str, float]:
    """The band in the cells a slider moves past their resolution: under half the move (never
    under the resolution), so the balance you drove is outside it and something changes.
    A smaller move leaves the band as it was: inside the dead zone."""
    out = dict(tolerance)
    for c in out:
        move = abs(shifted({c: 0.0}, front, rear, fast)[c])
        res = resolution.get(c, RES_MIN)
        if move > res:
            out[c] = max(res, min(out[c], 0.5 * move))
    return out


def deadzones(resolution: dict[str, float]) -> dict[str, float]:
    """Per axis, the smallest slider move that changes the band in some cell it covers."""
    out = {}
    for a in AXES:
        zs = [res / axis_weight(a, c) for c, res in resolution.items() if axis_weight(a, c) > 0]
        out[a] = round(min(zs), 3) if zs else 0.0
    return out


def weights_for(base: dict[str, float], mix: dict[str, float], fast: float) -> dict[str, float]:
    """Per cell, how much it counts here: its speed band's share of the cornering, and at
    least FAST_WEIGHT for the fast cells when you've asked something of them."""
    out = {}
    for c in base:
        band = cell_parts(c)[1]
        w = mix.get(band, 0.5)
        if band == "high" and abs(fast) > 1e-9:
            w = max(w, FAST_WEIGHT)
        out[c] = w
    return out


def target(store, car_key: str, car_class: str, knowledge: dict, current: int | None,
           layout_mix: dict[str, float] | None = None) -> Target:
    """The target for this car, ``current`` being the stint you're on: its sliders and,
    when one is moved or there's no fingerprint, its measurement anchor the target.
    ``layout_mix``: the layout's cornering time per speed band, which weights the cells here."""
    from autosetup.feel.fingerprint import anchor_for

    mv = sliders(store, current)
    step = float(knowledge["feel_step_deg"])
    moved = any(abs(v) > 1e-9 for v in mv.values())
    fp = anchor_for(store, car_key, car_class, knowledge)
    if fp is not None and not moved:
        base, tol = fp.cells, fp.tolerance
        res = {c: max(RES_MIN, 0.5 * t) for c, t in tol.items()}
        anchor, anchor_stint, fp_id, turnin, cs = "fingerprint", fp.recording_id, fp.id, fp.turnin, fp.countersteer
    else:
        if current is None:
            return Target(car_key, car_class, {}, {}, **mv, span=RANGE_STEPS * step)
        m = measured(store, same_setup_stints(store, current))
        base, tol, res = m.mean(), tolerance_of(m, knowledge), resolution_of(m)
        anchor = "moved" if moved else "current"
        anchor_stint, fp_id, turnin, cs = current, None, m.turnin, m.countersteer
    mix = layout_mix or {"low": 0.5, "high": 0.5}
    return Target(car_key, car_class, shifted(base, **mv), narrowed(tol, res, **mv), mv["front"], mv["rear"],
                  mv["fast"], anchor, anchor_stint, fp_id,
                  None if turnin is None else turnin - mv["front"] / step * TURNIN_PER_STEP,
                  None if cs is None else max(0.0, cs - (mv["rear"] + mv["fast"]) / step * COUNTERSTEER_PER_STEP),
                  weights_for(base, mix, mv["fast"]), deadzones(res), RANGE_STEPS * step)


def events_json(store, car_key: str) -> list[dict]:
    store.con.execute(SCHEMA)
    cur = store.con.execute("select kind, axis, step, recording_id, happened_at from feel_events where car_key = ? order by id",
                            [car_key])
    return [dict(zip(("kind", "axis", "step", "stint", "at"), (*r[:4], str(r[4])))) for r in cur.fetchall()]

