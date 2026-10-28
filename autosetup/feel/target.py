"""The balance the optimiser aims for, and the two toggles that move it.

Two axes, a toggle each, set on a stint:
  rear   Looser (−1) / As it was (0) / Safer (+1): mid-corner and exit rotation against
         rear grip margin (and how much snap / countersteer is acceptable)
  front  Pointier (−1) / As it was (0) / Lazier (+1): entry rotation and turn-in response
         against a calmer, more damped entry

A toggle asks for one step (``feel_step_deg``, in understeer-angle degrees: − is more
rotation) from how that stint felt: the target is the stint's balance moved a step in
the cells the axis covers, and the band there is kept under a step wide, so the setup
you drove is outside it and something changes. Each stint starts at As it was. With
nothing toggled the target is the car's locked fingerprint (fingerprint.py) or, without
one, what you drove. Toggles never move a setting themselves: the optimiser finds
settings that hit the target.

``feel_events`` keeps the locks and resets per car; ``feel_toggles`` the toggles per stint.
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
create table if not exists feel_toggles (
    recording_id  integer primary key,  -- the stint the toggles are about
    car_key       varchar,
    car_class     varchar,
    front         integer default 0,    -- −1 pointier, +1 lazier
    rear          integer default 0,    -- −1 looser, +1 safer
    set_at        timestamp
);
"""
TOGGLES = {"front": {"pointier": -1, "as it was": 0, "lazier": 1}, "rear": {"looser": -1, "as it was": 0, "safer": 1}}
# How much of an axis's step each phase gets (the cell's share of the toggle).
AXIS_WEIGHT = {"front": {"entry": 1.0, "mid": 0.3, "exit": 0.0},
               "rear": {"entry": 0.0, "mid": 0.7, "exit": 1.0}}
TURNIN_PER_STEP = 0.03  # turn-in response (|r| / |r_ref|) Pointier asks for more
COUNTERSTEER_PER_STEP = 0.5  # s per lap of countersteer Looser accepts more


@dataclass
class Target:
    car_key: str
    car_class: str
    cells: dict[str, float]  # deg, + understeer
    tolerance: dict[str, float]  # ± deg around each cell that is "it"
    front: int = 0  # toggle: −1 pointier, +1 lazier
    rear: int = 0  # −1 looser, +1 safer
    anchor: str = "none"  # fingerprint | toggled | current | none
    anchor_stint: int | None = None
    fingerprint: int | None = None
    turnin: float | None = None
    countersteer: float | None = None  # s per lap accepted
    weights: dict[str, float] = field(default_factory=dict)  # per cell: how much it counts here (corner mix)

    def off(self, values: dict[str, float]) -> dict[str, float]:
        """Per cell, how far outside the band a balance is, in tolerances (0 inside)."""
        out = {}
        for c, v in values.items():
            if c in self.cells:
                out[c] = max(0.0, abs(v - self.cells[c]) - self.tolerance[c]) / self.tolerance[c]
        return out

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("car_key", "car_class", "cells", "tolerance", "front", "rear", "anchor",
                                              "anchor_stint", "fingerprint", "turnin", "countersteer", "weights")}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def toggle(store, car_key: str, car_class: str, recording_id: int, axis: str, value: int) -> None:
    """Set an axis's toggle on a stint: −1, 0 (as it was) or +1."""
    store.con.execute(SCHEMA)
    if axis not in TOGGLES or value not in TOGGLES[axis].values():
        raise ValueError(f"axis must be front or rear and value -1, 0 or 1, not {axis!r}={value!r}")
    front, rear = toggles(store, recording_id)
    front, rear = (value, rear) if axis == "front" else (front, value)
    store.con.execute("insert or replace into feel_toggles values (?, ?, ?, ?, ?, ?)",
                      [recording_id, car_key, car_class, front, rear, _now()])


def toggles(store, recording_id: int | None) -> tuple[int, int]:
    """(front, rear) toggled on a stint; (0, 0) when nothing is."""
    store.con.execute(SCHEMA)
    if recording_id is None:
        return 0, 0
    row = store.con.execute("select front, rear from feel_toggles where recording_id = ?", [recording_id]).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def tolerance_of(m: Measured, knowledge: dict) -> dict[str, float]:
    """± deg per cell: at least ``feel_tolerance_deg``, wider where the measurement is noisy."""
    floor = float(knowledge["feel_tolerance_deg"])
    return {c: max(floor, 2.0 * v.se) for c, v in m.cells.items()}


def shifted(cells: dict[str, float], front: int, rear: int, step: float) -> dict[str, float]:
    out = {}
    for c, v in cells.items():
        ph, _ = cell_parts(c)
        out[c] = v + step * (front * AXIS_WEIGHT["front"][ph] + rear * AXIS_WEIGHT["rear"][ph])
    return out


def narrowed(tolerance: dict[str, float], front: int, rear: int, step: float) -> dict[str, float]:
    """The band in the cells a toggle moves by half a step or more: under half the move,
    so the balance you drove is outside it and the toggle always changes something."""
    out = dict(tolerance)
    for c in out:
        move = abs(shifted({c: 0.0}, front, rear, step)[c])
        if move >= 0.5 * step:
            out[c] = min(out[c], 0.5 * move)
    return out


def target(store, car_key: str, car_class: str, knowledge: dict, current: int | None,
           layout_mix: dict[str, float] | None = None) -> Target:
    """The target for this car, ``current`` being the stint you're on: its toggles and,
    when it has any or there's no fingerprint, its measurement anchor the target.
    ``layout_mix``: the layout's cornering time per speed band, which weights the cells here."""
    from autosetup.feel.fingerprint import anchor_for

    front, rear = toggles(store, current)
    step = float(knowledge["feel_step_deg"])
    fp = anchor_for(store, car_key, car_class, knowledge)
    if fp is not None and not (front or rear):
        base, tol = fp.cells, fp.tolerance
        anchor, anchor_stint, fp_id, turnin, cs = "fingerprint", fp.recording_id, fp.id, fp.turnin, fp.countersteer
    else:
        if current is None:
            return Target(car_key, car_class, {}, {}, front, rear)
        m = measured(store, same_setup_stints(store, current))
        base, tol = m.mean(), tolerance_of(m, knowledge)
        anchor = "toggled" if front or rear else "current"
        anchor_stint, fp_id, turnin, cs = current, None, m.turnin, m.countersteer
    mix = layout_mix or {"low": 0.5, "high": 0.5}
    return Target(car_key, car_class, shifted(base, front, rear, step), narrowed(tol, front, rear, step), front, rear,
                  anchor, anchor_stint, fp_id, None if turnin is None else turnin - front * TURNIN_PER_STEP,
                  None if cs is None else max(0.0, cs - rear * COUNTERSTEER_PER_STEP),
                  {c: mix.get(cell_parts(c)[1], 0.5) for c in base})


def events_json(store, car_key: str) -> list[dict]:
    store.con.execute(SCHEMA)
    cur = store.con.execute("select kind, axis, step, recording_id, happened_at from feel_events where car_key = ? order by id",
                            [car_key])
    return [dict(zip(("kind", "axis", "step", "stint", "at"), (*r[:4], str(r[4])))) for r in cur.fetchall()]

