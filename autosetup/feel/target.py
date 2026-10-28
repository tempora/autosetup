"""The balance the optimiser aims for, and the four buttons that move it.

Two axes, two buttons each:
  rear   Looser (−1) / Safer (+1): mid-corner and exit rotation against rear grip margin
         (and how much snap / countersteer is acceptable)
  front  Pointier (−1) / Lazier (+1): entry rotation and turn-in response against a calmer,
         more damped entry

The target is an anchor plus your presses. The anchor is your locked fingerprint for the
car (fingerprint.py) or, without one, the balance measured on the stint where you first
pressed since the last lock; with no presses and no lock it's simply what you're driving
now (no change asked for). Each press moves its axis by a fixed step (``feel_step_deg``,
in understeer-angle degrees: − is more rotation) in the cells it covers. Presses add up
and combine; they never move a setting themselves: the optimiser finds settings that hit
the new target.

Events are kept per car (feel_events): press, undo, lock, reset.
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
    kind          varchar,              -- press | lock | reset
    axis          varchar,              -- front | rear (press)
    step          integer,              -- -1 pointier/looser, +1 lazier/safer (press)
    recording_id  integer,              -- the stint you were on
    fingerprint   integer,              -- lock: the fingerprint it made
    happened_at   timestamp
);
"""
BUTTONS = {"pointier": ("front", -1), "lazier": ("front", 1), "looser": ("rear", -1), "safer": ("rear", 1)}
# How much of an axis's step each phase gets (the cell's share of the press).
AXIS_WEIGHT = {"front": {"entry": 1.0, "mid": 0.3, "exit": 0.0},
               "rear": {"entry": 0.0, "mid": 0.7, "exit": 1.0}}
TURNIN_PER_STEP = 0.03  # turn-in response (|r| / |r_ref|) a Pointier press asks for more
COUNTERSTEER_PER_STEP = 0.5  # s per lap of countersteer a Looser press accepts more


@dataclass
class Target:
    car_key: str
    car_class: str
    cells: dict[str, float]  # deg, + understeer
    tolerance: dict[str, float]  # ± deg around each cell that is "it"
    front: int = 0  # net presses: − pointier, + lazier
    rear: int = 0  # − looser, + safer
    anchor: str = "none"  # fingerprint | pressed | current | none
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


def press(store, car_key: str, car_class: str, button: str, recording_id: int | None) -> None:
    store.con.execute(SCHEMA)
    if button not in BUTTONS:
        raise ValueError(f"button must be one of {', '.join(BUTTONS)}")
    axis, step = BUTTONS[button]
    store.con.execute("insert into feel_events (car_key, car_class, kind, axis, step, recording_id, happened_at) "
                      "values (?, ?, 'press', ?, ?, ?, ?)", [car_key, car_class, axis, step, recording_id, _now()])


def undo(store, car_key: str) -> bool:
    """Take back the last press since the last lock or reset."""
    store.con.execute(SCHEMA)
    rows = _since_lock(store, car_key)
    presses = [r for r in rows if r["kind"] == "press"]
    if not presses:
        return False
    store.con.execute("delete from feel_events where id = ?", [presses[-1]["id"]])
    return True


def reset(store, car_key: str, car_class: str) -> None:
    """Drop the presses (the fingerprint, if any, stays the anchor)."""
    store.con.execute(SCHEMA)
    store.con.execute("insert into feel_events (car_key, car_class, kind, happened_at) values (?, ?, 'reset', ?)",
                      [car_key, car_class, _now()])


def _since_lock(store, car_key: str) -> list[dict]:
    cur = store.con.execute("select * from feel_events where car_key = ? order by id", [car_key])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    last = max((i for i, r in enumerate(rows) if r["kind"] in ("lock", "reset")), default=-1)
    return rows[last + 1:]


def steps(store, car_key: str) -> tuple[int, int, int | None]:
    """(front, rear, stint of the first press) since the last lock or reset."""
    store.con.execute(SCHEMA)
    presses = [r for r in _since_lock(store, car_key) if r["kind"] == "press"]
    front = sum(r["step"] for r in presses if r["axis"] == "front")
    rear = sum(r["step"] for r in presses if r["axis"] == "rear")
    return front, rear, (presses[0]["recording_id"] if presses else None)


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


def target(store, car_key: str, car_class: str, knowledge: dict, current: int | None,
           layout_mix: dict[str, float] | None = None) -> Target:
    """The target for this car, ``current`` being the stint you're on (its measurement
    anchors a target with no fingerprint). ``layout_mix``: the layout's cornering time per
    speed band, which weights the cells here."""
    from autosetup.feel.fingerprint import anchor_for

    front, rear, first = steps(store, car_key)
    step = float(knowledge["feel_step_deg"])
    fp = anchor_for(store, car_key, car_class, knowledge)
    if fp is not None:
        base, tol = fp.cells, fp.tolerance
        anchor, anchor_stint, fp_id, turnin, cs = "fingerprint", fp.recording_id, fp.id, fp.turnin, fp.countersteer
    else:
        rid = first if first is not None else current
        if rid is None:
            return Target(car_key, car_class, {}, {}, front, rear)
        m = measured(store, same_setup_stints(store, rid))
        base, tol = m.mean(), tolerance_of(m, knowledge)
        anchor = "pressed" if first is not None else "current"
        anchor_stint, fp_id, turnin, cs = rid, None, m.turnin, m.countersteer
    mix = layout_mix or {"low": 0.5, "high": 0.5}
    return Target(car_key, car_class, shifted(base, front, rear, step), dict(tol), front, rear, anchor, anchor_stint,
                  fp_id, None if turnin is None else turnin - front * TURNIN_PER_STEP,
                  None if cs is None else max(0.0, cs - rear * COUNTERSTEER_PER_STEP),
                  {c: mix.get(cell_parts(c)[1], 0.5) for c in base})


def events_json(store, car_key: str) -> list[dict]:
    store.con.execute(SCHEMA)
    cur = store.con.execute("select kind, axis, step, recording_id, happened_at from feel_events where car_key = ? order by id",
                            [car_key])
    return [dict(zip(("kind", "axis", "step", "stint", "at"), (*r[:4], str(r[4])))) for r in cur.fetchall()]

