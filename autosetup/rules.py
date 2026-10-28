"""Rules for what has a clear target, outside the optimiser, with one plain reason each:

  * tyres    pressure per wheel from the tread crown (centre vs edges; pressure only ever
             comes down), camber per axle from the inner-outer spread;
  * brakes   front/rear ducts from peak disc temperature; brake pressure by feel on cars
             without ABS (up a step per stint until you say it's too much);
  * platform ride height up when the car bottoms or runs under the class minimum; on
             cars with a third spring, its packers first.

Balance, aero and the dampers are the optimiser's (opt/bo.py, opt/dampers.py).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field

from autosetup import units
from autosetup.analysis import pretty_name
from autosetup.knowledge import KNOWLEDGE, load_knowledge  # noqa: F401 (re-exported)
from autosetup.lmu.setup_space import CarSetup
from autosetup.scales import CarScales

DISABLING = {"detached", "off", "n/a", "disabled"}  # moves never switch something off


@dataclass
class Change:
    group: str
    keys: list[str]
    before: dict[str, int]
    after: dict[str, int]
    reason: str
    name: str = ""
    before_text: str = ""
    after_text: str = ""
    approx: bool = False  # after_text interpolated, not seen before
    source: str = "rules"  # rules | rating | telemetry | model | learning

    @property
    def delta(self) -> int:
        k = self.keys[0]
        return self.after[k] - self.before[k]


@dataclass
class Proposal:
    stint_id: int
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    model: dict | None = None  # optimiser state shown in the Model tab

    @property
    def values(self) -> dict[str, int]:
        return {k: v for c in self.changes for k, v in c.after.items()}

    @property
    def signature(self) -> str:
        body = ",".join(f"{k}={v}" for k, v in sorted(self.values.items()))
        return hashlib.sha1(f"{self.stint_id}|{body}".encode()).hexdigest()[:12]

    def to_json(self) -> dict:
        return {
            "stint_id": self.stint_id,
            "signature": self.signature,
            "notes": self.notes,
            "changes": [dict(asdict(c), delta=c.delta) for c in self.changes],
            "model": self.model,
        }


# -- direction helpers ---------------------------------------------------------------
# Index direction for "more" of each setting. Numeric settings follow their labels (a
# brake-balance step up moves bias rearward, because its label reads front %); the rest
# follow the game's convention that a higher index is stiffer / more / more closed.
def _index_dir(key: str, scales: CarScales, numeric_up: bool) -> int:
    step = scales[key].per_step
    sign = 1 if step is None or step > 0 else -1
    return sign if numeric_up else -sign


SEMANTIC = {
    # (key, intent) -> function(scales) giving the index direction
    "stiffer": lambda key, sc: 1,
    "softer": lambda key, sc: -1,
    "more": lambda key, sc: 1,
    "less": lambda key, sc: -1,
    "rear": lambda key, sc: -_index_dir(key, sc, numeric_up=True),  # front % down
    "front": lambda key, sc: _index_dir(key, sc, numeric_up=True),
    "up": lambda key, sc: _index_dir(key, sc, numeric_up=True),
    "down": lambda key, sc: _index_dir(key, sc, numeric_up=False),
    "open": lambda key, sc: -1,  # duct index 0 = Open
    "close": lambda key, sc: 1,
}

FRONT_RH = ["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"]
REAR_RH = ["WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"]
# Slow dampers shape the weight transfer of each phase: on the brakes the front is in bump
# and the rear in rebound, on power the other way round. Damping an end harder moves load
# across it sooner, which costs that end grip in the transition.
# -- the rules -------------------------------------------------------------------------
class _Builder:
    def __init__(self, setup: CarSetup, scales: CarScales, locks: set[str]):
        self.setup, self.scales, self.locks = setup, scales, locks
        self.changes: list[Change] = []
        self.notes: list[str] = []
        self.moved: dict[str, int] = {}  # key -> direction already used

    def usable(self, key: str) -> bool:
        p = self.setup.params.get(key)
        return p is not None and p.adjustable and key not in self.locks

    def add(self, group: str, keys: list[str], direction: int, steps: int, reason: str, name: str | None = None,
            quiet: bool = False) -> bool:
        """Queue a move. ``quiet`` skips the "at its limit" note (corrections like tyre
        pressure routinely sit at the minimum; saying so every time is noise)."""
        keys = [k for k in keys if k in self.setup.params]
        if not keys:
            return False
        if any(k in self.locks for k in keys):
            self.notes.append(f"{name or pretty_name(keys[0])} is locked")
            return False
        if not all(self.usable(k) for k in keys):
            return False
        if any(k in self.moved for k in keys):
            return False  # another rule already moves it
        before = {k: self.setup[k].value for k in keys}
        after = {k: self.setup[k].clamp(before[k] + direction * steps) for k in keys}
        if after == before:
            if not quiet:
                self.notes.append(f"{name or pretty_name(keys[0])} is already at its limit")
            return False
        p = self.setup[keys[0]]
        label, exact = self.scales.label(keys[0], after[keys[0]])
        if group == "balance" and label and label.strip().lower().split(" ")[0] in DISABLING:
            return False  # e.g. a 'softer' bar that would detach it: try the next option
        for k in keys:
            self.moved[k] = direction
        if label is None:
            d = after[keys[0]] - before[keys[0]]
            label = f"{p.text.strip()} {'+' if d > 0 else '−'}{abs(d)} step{'s' if abs(d) > 1 else ''}"
        display = name or (pretty_name(keys[0]) + ("/" + "/".join(k[-2:] for k in keys[1:]) if len(keys) > 1 else ""))
        self.changes.append(Change(group, keys, before, after, reason, display, p.text.strip(), label, approx=not exact))
        return True


def propose(stint_id: int, summary: dict, setup: CarSetup, car_class: str, scales: CarScales,
            rating: dict | None = None, locks: set[str] | None = None, knowledge: dict | None = None,
            brake_ceiling: int | None = None) -> Proposal:
    """The rules' changes for the next stint. ``brake_ceiling``: highest brake pressure
    index this car may go to (one step under where you said it was too much)."""
    k = knowledge or load_knowledge(car_class)
    b = _Builder(setup, scales, set(locks or ()))
    _tyres(b, summary, k)
    _brakes(b, summary, k)
    _brake_pressure(b, k, rating or {}, brake_ceiling, 1)
    _platform(b, summary, k)
    order = {"tyres": 0, "brakes": 1, "platform": 2}
    b.changes.sort(key=lambda c: order[c.group])
    return Proposal(stint_id, b.changes, b.notes)


def _num(summary: dict, *keys: str) -> list[float]:
    return [summary[x] for x in keys if summary.get(x) is not None]


def _tyres(b: _Builder, s: dict, k: dict) -> None:
    target, band = float(k["crown_target"]), float(k["crown_band"])
    hot = k.get("hot_pressure") or []
    # Pressure per wheel; pairs with the same move are merged into one row. Pressure only
    # ever comes down: the minimum is where LMU's tyres want to be, so a cool tread centre
    # is never a reason to raise it.
    moves: dict[tuple[str, int, int], list[str]] = {}
    for axle, wheels in (("front", ("FL", "FR")), ("rear", ("RL", "RR"))):
        for w in wheels:
            crown = s.get(f"t_crown_{w.lower()}")
            p_hot = s.get(f"p_hot_{w.lower()}")
            up, steps = 0, 0
            if crown is not None and abs(crown - target) > band:
                up = -1 if crown > target else 1
                steps = min(3, math.ceil((abs(crown - target) - band) / 1.5))
            elif len(hot) == 2 and p_hot is not None and not hot[0] <= p_hot <= hot[1]:
                up = 1 if p_hot < hot[0] else -1
                steps = min(3, math.ceil(abs(p_hot - (hot[0] if up > 0 else hot[1])) / 2))
            if up < 0:
                moves.setdefault((axle, up, steps), []).append(w)
    for (axle, up, steps), wheels in moves.items():
        keys = [f"WM_PRESSURE-W_{w}" for w in wheels]
        crowns = _num(s, *(f"t_crown_{w.lower()}" for w in wheels))
        avg = sum(crowns) / len(crowns) if crowns else target
        unit = b.scales[keys[0]].per_step
        u = units.current
        size = f" ({'+' if up > 0 else '−'}{u.fmt_pressure(abs(unit * steps))})" if unit else ""
        if abs(avg - target) > band:
            why = (f"Tread centre {u.fmt_temp(avg, delta=True, sign=True)} against the edges "
                   f"(normal {u.temp_range(target - band, target + band, delta=True)})")
        else:
            hots = _num(s, *(f"p_hot_{w.lower()}" for w in wheels))
            p_lo, p_hi = u.pressure_value(hot[0]), u.pressure_value(hot[1])
            why = f"Hot pressure {u.fmt_pressure(sum(hots) / len(hots))} (target {p_lo:.4g}–{p_hi:.4g})"
        b.add("tyres", keys, SEMANTIC["up"](keys[0], b.scales) * up, steps,
              f"{why} → pressure {'up' if up > 0 else 'down'}{size}", quiet=True)
    # Camber per axle.
    for axle, wheels, band_key in (("Front", ("FL", "FR"), "spread_front"), ("Rear", ("RL", "RR"), "spread_rear")):
        spreads = _num(s, *(f"t_spread_{w.lower()}" for w in wheels))
        if len(spreads) < 2:
            continue
        lo, hi = k[band_key]
        spread = sum(spreads) / 2
        if lo <= spread <= hi:
            continue
        less_camber = spread > hi  # too much inner heat → less negative camber (value up)
        steps = 2 if abs(spread - (hi if less_camber else lo)) > 3 else 1
        keys = [f"WM_CAMBER-W_{w}" for w in wheels]
        b.add("tyres", keys, SEMANTIC["up" if less_camber else "down"](keys[0], b.scales), steps,
              f"{axle} inner edge {units.current.fmt_temp(spread, delta=True)} hotter than outer "
              f"(target {units.current.temp_range(lo, hi, delta=True)}) → "
              f"{'less' if less_camber else 'more'} negative camber", quiet=True)


def _brakes(b: _Builder, s: dict, k: dict) -> None:
    lo, hi = k["brake_temp"]
    for axle, key, wheels in (("Front", "VM_BRAKE_DUCTS", ("fl", "fr")), ("Rear", "VM_BRAKE_DUCTS_REAR", ("rl", "rr"))):
        temps = _num(s, *(f"brake_temp_max_{w}" for w in wheels))
        if not temps:
            continue
        peak = max(temps)
        u = units.current
        if peak > hi:
            b.add("brakes", [key], SEMANTIC["open"](key, b.scales), 1,
                  f"{axle} brakes peak at {u.fmt_temp(peak, 0)} (target under {u.temp_value(hi):.0f}) → open the ducts a step",
                  quiet=True)
        elif peak < lo:
            b.add("brakes", [key], SEMANTIC["close"](key, b.scales), 1,
                  f"{axle} brakes peak at only {u.fmt_temp(peak, 0)} (target over {u.temp_value(lo):.0f}) → close the ducts a step",
                  quiet=True)


BRAKE_PRESSURE_STEP = 2  # clicks per stint (1 kgf each on LMU's cars), times the change size's multiplier


def manages_brake_pressure(setup: CarSetup, k: dict) -> bool:
    """Brake pressure is set by feel here: the car has no ABS and its class a comfortable
    range (``brake_pressure_pct``). Cars with ABS keep theirs."""
    p = setup.params.get("VM_BRAKE_PRESSURE")
    abs_map = setup.params.get("VM_ANTILOCKBRAKESYSTEMMAP")
    return (len(k.get("brake_pressure_pct") or []) == 2 and p is not None and p.adjustable
            and p.number is not None and not (abs_map is not None and abs_map.adjustable))


def _brake_pressure(b: _Builder, k: dict, rating: dict, ceiling: int | None, mult: int) -> None:
    """Brake pressure where it's set by feel (manages_brake_pressure), inside the class's
    comfortable range: up a step every stint until you say it's too much, then down a step
    and never back above that (``ceiling``, learned per car model). Below the range it goes
    straight to the bottom of it, above it down to the top.

    Percent is of the setting's highest index (LMU's cars: 120 kgf = 100 %, 1 kgf a click,
    a higher index = more pressure)."""
    key = "VM_BRAKE_PRESSURE"
    if not manages_brake_pressure(b.setup, k) or not b.usable(key):
        return
    rng = k["brake_pressure_pct"]
    p = b.setup[key]
    per_click = abs(b.scales[key].per_step or 1.0)
    full = p.number + (p.max - 1 - p.value) * per_click

    def pct(i: int) -> float:
        return 100.0 * (p.number + (i - p.value) * per_click) / full

    def index(at: float, rnd) -> int:  # rnd: ceil for the bottom of the range, floor for the top
        return p.clamp(p.value + rnd(round((at / 100.0 * full - p.number) / per_click, 6)))

    lo, hi = index(rng[0], math.ceil), index(rng[1], math.floor)
    top = min(hi, ceiling) if ceiling is not None else hi
    step = BRAKE_PRESSURE_STEP * mult
    usual = f"usual {rng[0]:g}–{rng[1]:g} %"
    if rating.get("lockups"):
        b.add("brakes", [key], -1, step, f"You said {pct(p.value):.0f} % is too much brake pressure → down a step")
    elif p.value > top:
        why = (f"over where you said it was too much ({pct(ceiling + 1):.0f} %)" if top == ceiling
               else f"above the {usual}")
        b.add("brakes", [key], -1, p.value - top, f"Brake pressure {pct(p.value):.0f} % is {why} → down to {pct(top):.0f} %")
    elif p.value < lo and lo <= top:
        b.add("brakes", [key], 1, lo - p.value, f"Brake pressure {pct(p.value):.0f} % is below the {usual} → up to {pct(lo):.0f} %")
    elif p.value < top:
        b.add("brakes", [key], 1, min(step, top - p.value),
              f"{pct(p.value):.0f} % wasn't too much → brake pressure up a step "
              f"(press Too much brake pressure when it is)", quiet=True)


def _platform(b: _Builder, s: dict, k: dict) -> None:
    """Ride height from bottoming. On prototypes, a car that bottoms while hardly
    touching its third packers gets a packer step instead of a higher ride height (which
    costs downforce). Lowering a car with room to spare is the optimiser's (it knows what
    the downforce is worth, inside the same platform limits)."""
    budget = int(k["max_platform_changes"])
    front_rh = _num(s, "rh_min_fl", "rh_min_fr")
    rear_rh = _num(s, "rh_min_rl", "rh_min_rr")
    bottoming = s.get("bottoming_s") or 0.0
    done = 0
    front_low = bool(front_rh) and (bottoming > k["bottoming_s"] or min(front_rh) < k["min_ride_height_mm"])
    rear_low = bool(rear_rh) and min(rear_rh) < k["min_ride_height_mm"]
    if front_low:
        steps = 2 if bottoming > 3 * k["bottoming_s"] else 1
        why = (f"front bottoms {bottoming:.2f} s/lap" if bottoming > k["bottoming_s"]
               else f"front ride height drops to {min(front_rh):.1f} mm")
        done += (_packers_in(b, s, k, "front", f"The {why}")
                 or b.add("platform", FRONT_RH, SEMANTIC["up"](FRONT_RH[0], b.scales), steps,
                          f"The {why} → raise the front", quiet=True))
    if done < budget and rear_low:
        why = f"The rear ride height drops to {min(rear_rh):.1f} mm"
        done += (_packers_in(b, s, k, "rear", why)
                 or b.add("platform", REAR_RH, SEMANTIC["up"](REAR_RH[0], b.scales), 1, f"{why} → raise the rear",
                          quiet=True))


def _packers_in(b: _Builder, s: dict, k: dict, axle: str, why: str) -> bool:
    """One more step of third packer on an axle that bottoms but hardly uses them."""
    frac = s.get(f"third_packers_{axle[0]}")
    key = f"VM_{axle.upper()}_3RD_PACKERS"
    if frac is None or frac >= k["third_packers_lo"] or not b.usable(key):
        return False
    return b.add("platform", [key], SEMANTIC["more"](key, b.scales), 1,
                 f"{why} while its third spring is on the packers only {frac:.0%} of the lap "
                 f"→ one step more {axle} third packer", quiet=True)
