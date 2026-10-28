"""Rules-based advisor: from one stint's metrics (and your optional rating) to the next
setup, with one plain reason per change.

Groups and how they're decided:
  * tyres    pressure per wheel from the tread crown (centre vs edges), camber per axle
             from the inner-outer spread;
  * brakes   front/rear ducts from peak disc temperature;
  * platform ride height from bottoming (and, when pace matters, lowering a car that
             has room to spare); on cars with a third spring, its packers first;
  * balance  per corner phase (entry, mid, exit). Your rating decides when given;
             otherwise telemetry: countersteer beyond what you've shown you're fine with
             (learned from your ratings), or an understeer angle
             a rating point or more away from the angle you like (learned from your
             ratings, opt/balance.py). Tools include bars, brake bias, diff, slow
             dampers, ride height (rake) and rear wing; a complaint that persists gets
             a different tool than the one changed last time. Traction control that
             cuts in a lot while the exit doesn't oversteer is turned down a step.

How much one proposal moves is your "change per stint" choice (small/medium/large).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from autosetup import units
from autosetup.analysis import pretty_name
from autosetup.knowledge import KNOWLEDGE, load_knowledge  # noqa: F401 (re-exported)
from autosetup.lmu.setup_space import CarSetup
from autosetup.opt.balance import CS_MARGIN, Target, zone_ratings
from autosetup.opt.balance import learn as learn_balance
from autosetup.opt.dataset import zone_parts
from autosetup.scales import CarScales

MIN_RACING_LAPS = 2
PHASES = ("entry", "mid", "exit")
SPEEDS = ("slow", "medium", "fast")  # corner speed bands
ZONES = tuple(f"{sp}_{ph}" for sp in SPEEDS for ph in PHASES)  # = opt.dataset.ZONES
DISABLING = {"detached", "off", "n/a", "disabled"}  # balance moves never switch something off



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
FRONT_SB = ["WM_SLOWBUMP-W_FL", "WM_SLOWBUMP-W_FR"]
FRONT_SR = ["WM_SLOWREBOUND-W_FL", "WM_SLOWREBOUND-W_FR"]
REAR_SB = ["WM_SLOWBUMP-W_RL", "WM_SLOWBUMP-W_RR"]
REAR_SR = ["WM_SLOWREBOUND-W_RL", "WM_SLOWREBOUND-W_RR"]
# Per phase and problem, the tools in order of preference. Settings changed in the last
# couple of stints go to the back of the queue, so a complaint that persists gets a
# different tool instead of the same bar softened again.
BALANCE_ACTIONS = {
    ("entry", "under"): [(["VM_BRAKE_BALANCE"], "rear", "brake bias rearward"),
                         (REAR_RH, "up", "raise rear ride height (more rake, more front grip)"),
                         (["VM_FRONT_TOEIN"], "less", "more front toe-out (sharpens turn-in response)"),
                         (REAR_SR, "stiffer", "stiffer rear slow rebound (the rear unloads sooner on the brakes)"),
                         (["VM_FRONT_ANTISWAY"], "softer", "softer front anti-roll bar"),
                         (FRONT_RH, "down", "lower front ride height (more front downforce)"),
                         (FRONT_SB, "softer", "softer front slow bump (the front takes load more gently)")],
    ("entry", "over"): [(["VM_BRAKE_BALANCE"], "front", "brake bias forward"),
                        (["VM_REAR_TOEIN"], "more", "more rear toe-in (stabilizes the rear on entry)"),
                        (REAR_SR, "softer", "softer rear slow rebound (the rear stays loaded on the brakes)"),
                        (["VM_DIFF_COAST"], "more", "more diff coast lock"),
                        (["VM_REAR_ANTISWAY"], "softer", "softer rear anti-roll bar"),
                        (FRONT_SB, "stiffer", "stiffer front slow bump")],
    ("mid", "under"): [(REAR_RH, "up", "raise rear ride height (more rake, more front grip)"),
                       (["VM_FRONT_ANTISWAY"], "softer", "softer front anti-roll bar"),
                       (["VM_REAR_ANTISWAY"], "stiffer", "stiffer rear anti-roll bar"),
                       (FRONT_RH, "down", "lower front ride height (more front downforce)"),
                       (["VM_REAR_WING"], "down", "less rear wing"),
                       (["VM_LEFT_CASTER", "VM_RIGHT_CASTER"], "more", "more caster (more dynamic front camber in corners)")],
    ("mid", "over"): [(["VM_REAR_ANTISWAY"], "softer", "softer rear anti-roll bar"),
                      (["VM_FRONT_ANTISWAY"], "stiffer", "stiffer front anti-roll bar"),
                      (["VM_REAR_WING"], "up", "more rear wing")],
    ("exit", "under"): [(["VM_DIFF_POWER"], "less", "less diff power lock"),
                        (REAR_RH, "up", "raise rear ride height (more rake, more front grip)"),
                        (FRONT_SR, "stiffer", "stiffer front slow rebound (the front stays down on power)"),
                        (["VM_DIFF_PRELOAD"], "less", "less diff preload"),
                        (["VM_FRONT_ANTISWAY"], "softer", "softer front anti-roll bar"),
                        (["VM_REAR_WING"], "down", "less rear wing"),
                        (["VM_REAR_TOEIN"], "less", "less rear toe-in (frees up the rear to rotate)")],
    ("exit", "over"): [(["VM_TRACTIONCONTROLMAP"], "more", "more traction control"),
                       (["VM_TRACTIONCONTROLSLIPANGLEMAP"], "more", "stricter TC slip angle (catch exit slides sooner)"),
                       (["VM_TRACTIONCONTROLPOWERCUTMAP"], "more", "more TC power cut"),
                       (["VM_REAR_TOEIN"], "more", "more rear toe-in (stabilizes the rear on power)"),
                       (REAR_SB, "softer", "softer rear slow bump (the rear squats and grips on power)"),
                       (["VM_DIFF_PRELOAD"], "more", "more diff preload"),
                       (["VM_REAR_ANTISWAY"], "softer", "softer rear anti-roll bar"),
                       (["VM_REAR_WING"], "up", "more rear wing")],
    # By corner speed: slow corners are mechanical grip (bars, springs, diff), fast ones
    # aero (rake, wings), medium a mix.
    ("slow", "under"): [(["VM_FRONT_ANTISWAY"], "softer", "softer front anti-roll bar"),
                        (["VM_REAR_ANTISWAY"], "stiffer", "stiffer rear anti-roll bar"),
                        (["VM_DIFF_PRELOAD"], "less", "less diff preload"),
                        (["WM_SPRING-W_FL", "WM_SPRING-W_FR"], "softer", "softer front springs"),
                        (["VM_FRONT_TOEIN"], "less", "more front toe-out (sharpens turn-in response)")],
    ("slow", "over"): [(["VM_REAR_ANTISWAY"], "softer", "softer rear anti-roll bar"),
                       (["VM_FRONT_ANTISWAY"], "stiffer", "stiffer front anti-roll bar"),
                       (["VM_DIFF_PRELOAD"], "more", "more diff preload"),
                       (["WM_SPRING-W_RL", "WM_SPRING-W_RR"], "softer", "softer rear springs"),
                       (["VM_TRACTIONCONTROLMAP"], "more", "more traction control")],
    ("medium", "under"): [(REAR_RH, "up", "raise rear ride height (more rake, more front grip)"),
                          (["VM_FRONT_ANTISWAY"], "softer", "softer front anti-roll bar"),
                          (["VM_REAR_WING"], "down", "less rear wing"),
                          (["VM_REAR_ANTISWAY"], "stiffer", "stiffer rear anti-roll bar")],
    ("medium", "over"): [(["VM_REAR_WING"], "up", "more rear wing"),
                         (["VM_REAR_ANTISWAY"], "softer", "softer rear anti-roll bar"),
                         (REAR_RH, "down", "lower rear ride height (less rake, more rear grip)"),
                         (["VM_FRONT_ANTISWAY"], "stiffer", "stiffer front anti-roll bar")],
    ("fast", "under"): [(REAR_RH, "up", "raise rear ride height (more rake, more front downforce)"),
                        (["VM_REAR_WING"], "down", "less rear wing"),
                        (["VM_FRONT_WING"], "up", "more front wing"),
                        (FRONT_RH, "down", "lower front ride height (more front downforce)")],
    ("fast", "over"): [(["VM_REAR_WING"], "up", "more rear wing"),
                       (REAR_RH, "down", "lower rear ride height (less rake, more rear downforce)"),
                       (["VM_FRONT_WING"], "down", "less front wing")],
}
MECHANICAL = PHASES + ("slow", "medium")
AERO = PHASES + ("medium", "fast")
# What adds understeer, per setting and zone (corner phase or speed): the direction the
# balance model uses (opt/balance.py). Only where the car sits now is measured; the size is
# a fixed share of each setting's range, not learned (it doesn't carry between tracks).
UNDERSTEER = [
    (["VM_FRONT_ANTISWAY"], "stiffer", MECHANICAL),
    (["VM_REAR_ANTISWAY"], "softer", MECHANICAL),
    (["WM_SPRING-W_FL", "WM_SPRING-W_FR"], "stiffer", MECHANICAL),
    (["WM_SPRING-W_RL", "WM_SPRING-W_RR"], "softer", MECHANICAL),
    (["VM_BRAKE_BALANCE"], "front", ("entry",)),
    (["VM_DIFF_COAST"], "more", ("entry",)),
    (["VM_DIFF_PRELOAD"], "more", ("entry", "exit", "slow")),
    (["VM_DIFF_POWER"], "more", ("exit", "slow")),
    (["VM_DIFF_PUMP"], "more", ("exit",)),
    (["VM_REAR_WING"], "up", ("mid", "exit", "medium", "fast")),
    (["VM_FRONT_WING"], "down", ("mid", "medium", "fast")),
    (FRONT_RH, "up", PHASES + SPEEDS),
    (REAR_RH, "down", PHASES + SPEEDS),
    (FRONT_SB, "stiffer", ("entry",)),
    (REAR_SR, "softer", ("entry",)),
    (FRONT_SR, "softer", ("exit",)),
    (REAR_SB, "stiffer", ("exit",)),
    (["VM_FRONT_TOEIN"], "more", ("entry", "mid", "slow")),
    (["VM_REAR_TOEIN"], "more", ("entry", "exit", "slow")),
    (["VM_LEFT_CASTER", "VM_RIGHT_CASTER"], "less", ("entry", "mid", "slow")),
    (["WM_CAMBER-W_FL", "WM_CAMBER-W_FR"], "up", ("mid", "slow", "medium")),
    (["WM_CAMBER-W_RL", "WM_CAMBER-W_RR"], "down", ("mid", "exit", "slow", "medium")),
]


def understeer_signs(scales: CarScales) -> dict[str, dict[str, int]]:
    """Per setting key and zone (corner speed × phase): +1 if a higher index adds
    understeer, -1 if it adds rotation (directions follow each car's labels, as the rules
    do). An UNDERSTEER entry naming phases and speeds covers their combinations; one naming
    only phases (or only speeds) covers them at every speed (or in every phase)."""
    out: dict[str, dict[str, int]] = {}
    for keys, intent, zones in UNDERSTEER:
        phases = [z for z in zones if z in PHASES] or list(PHASES)
        speeds = [z for z in zones if z in SPEEDS] or list(SPEEDS)
        for key in keys:
            sign = SEMANTIC[intent](key, scales)
            out.setdefault(key, {}).update({f"{sp}_{ph}": sign for sp in speeds for ph in phases})
    return out


# How much one proposal may change, chosen by you (see opt.optimizer.CHANGE_SIZES):
# balance moves per proposal and how many clicks one rule move is.
RULE_SIZE = {"small": (2, 1), "medium": (3, 1), "large": (4, 2)}
PHASE_WORDS = {"entry": "Entry", "mid": "Mid-corner", "exit": "Exit",
               "slow": "Low-speed corners", "medium": "Mid-speed corners", "fast": "High-speed corners"}
_SPEED_WORDS = {"slow": "Low-speed", "medium": "Mid-speed", "fast": "High-speed"}
_PHASE_NOUNS = {"entry": "corner entries", "mid": "mid-corner", "exit": "corner exits"}
PHASE_WORDS |= {f"{sp}_{ph}": f"{_SPEED_WORDS[sp]} {_PHASE_NOUNS[ph]}" for sp in SPEEDS for ph in PHASES}


def _steps_for(key: str, severity: float, scales: CarScales, mult: int = 1) -> int:
    """Size of one balance move: about 0.5 % brake bias, 10 Nm preload, else one click
    per severity level (rating points, halves allowed); ``mult`` scales it for larger
    change sizes."""
    step = scales[key].per_step
    if key == "VM_BRAKE_BALANCE":
        return max(1, round(0.5 * severity * mult / abs(step))) if step else max(1, round(2 * severity * mult))
    if key == "VM_DIFF_PRELOAD":
        return max(1, round(10 * severity * mult / abs(step))) if step else max(1, round(5 * severity * mult))
    if "TRACTIONCONTROL" in key:
        return 1
    if "TOEIN" in key:
        return max(1, round(0.05 * severity * mult / abs(step))) if step else max(1, round(severity * mult))
    if key in REAR_RH:
        # ~2 mm a severity level (steps are cm): one 1 mm click of rake changes nothing, the
        # understeer stays, and the next stint piles another tool on top. The front keeps
        # single clicks: lowering it is bounded by bottoming.
        return max(1, round(0.2 * severity * mult / abs(step))) if step else max(1, round(2 * severity * mult))
    return max(1, min(round(severity * mult), 3))


def _zone_actions(zone: str, kind: str) -> list:
    """The tools for a problem in one zone: its corner phase's, ordered for its corner speed
    (slow corners mechanical first, fast ones aero first, medium the phase's own order)."""
    if zone in PHASES or zone in SPEEDS:
        return BALANCE_ACTIONS[(zone, kind)]
    sp, ph = zone.split("_", 1)
    phase, speed = BALANCE_ACTIONS[(ph, kind)], BALANCE_ACTIONS[(sp, kind)]
    first, then = (phase, speed) if sp == "medium" else (speed, phase)
    out, seen = [], set()
    for a in first + then:
        if (tuple(a[0]), a[1]) not in seen:
            seen.add((tuple(a[0]), a[1]))
            out.append(a)
    return out


# -- the advisor -----------------------------------------------------------------------
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
            rating: dict | None = None, weights: dict | None = None, locks: set[str] | None = None,
            knowledge: dict | None = None, size: str = "large", recent: set[str] | None = None,
            targets: dict[str, Target] | None = None, countersteer: dict[str, float] | None = None,
            brake_ceiling: int | None = None, wing_effect: float = 0.0) -> Proposal:
    """``recent``: settings changed in the last couple of stints; ``size``: small, medium
    or large (how much one proposal may change); ``targets``: the understeer angle you
    like per phase (opt/balance.py), the knowledge file's when not given;
    ``countersteer``: countersteer moments per lap per phase you're fine with
    (Balance.tolerance), the knowledge file's threshold when not given;
    ``brake_ceiling``: highest brake pressure index this car may go to (one step under
    where you said it was too much), none when you never have; ``wing_effect``: seconds
    this track's lap loses from least to most rear wing (opt/drag.py)."""
    k = knowledge or load_knowledge(car_class)
    w = {"pace": 1.0, "tyres": 0.5, "drivability": 0.75, **(weights or {})}
    b = _Builder(setup, scales, set(locks or ()))
    if (summary.get("racing_laps") or 0) < MIN_RACING_LAPS:
        return Proposal(stint_id, notes=[f"Drive at least {MIN_RACING_LAPS} full laps for a proposal."])

    _balance(b, summary, rating or {}, k, w, size, recent or set(), targets or learn_balance([], k).targets,
             countersteer or learn_balance([], k).tolerance(k), wing_effect)
    if w["tyres"] > 0:
        _tyres(b, summary, k)
    _brakes(b, summary, k)
    _brake_pressure(b, k, rating or {}, brake_ceiling, RULE_SIZE.get(size, RULE_SIZE["large"])[1])
    _platform(b, summary, k, w)
    order = {"balance": 0, "tyres": 1, "brakes": 2, "platform": 3}
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


WING_COSTLY_S = 0.15  # wing_effect (s across its range) from which the costly direction goes last
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


def _platform(b: _Builder, s: dict, k: dict, w: dict) -> None:
    """Ride height from bottoming. On prototypes, a car that bottoms while hardly
    touching its third packers gets a packer step instead of a higher ride height (which
    costs downforce). How much packer suits you otherwise is left to the model."""
    budget = int(k["max_platform_changes"])
    front_rh = _num(s, "rh_min_fl", "rh_min_fr")
    rear_rh = _num(s, "rh_min_rl", "rh_min_rr")
    bottoming = s.get("bottoming_s") or 0.0
    done = 0
    front_low = bool(front_rh) and (bottoming > k["bottoming_s"] or min(front_rh) < k["min_ride_height_mm"])
    rear_low = bool(rear_rh) and min(rear_rh) < k["min_ride_height_mm"]
    if front_low:
        steps = 2 if bottoming > 3 * k["bottoming_s"] else 1
        why = f"front bottoms {bottoming:.2f} s/lap" if bottoming > k["bottoming_s"] else             f"front ride height drops to {min(front_rh):.1f} mm"
        done += _packers_in(b, s, k, "front", f"The {why}") or             b.add("platform", ["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"], SEMANTIC["up"]("WM_RIDEHEIGHT-W_FL", b.scales),
                  steps, f"The {why} → raise the front", quiet=True)
    if done < budget and rear_low:
        why = f"The rear ride height drops to {min(rear_rh):.1f} mm"
        done += _packers_in(b, s, k, "rear", why) or             b.add("platform", ["WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"], SEMANTIC["up"]("WM_RIDEHEIGHT-W_RL", b.scales),
                  1, f"{why} → raise the rear", quiet=True)
    # Rake is a balance tool, not a platform correction: the understeer rules raise the
    # rear, and the optimiser models what rear ride height does to your rotation.
    if done < budget and w["pace"] >= 0.6 and bottoming <= k["bottoming_s"]:
        if front_rh and min(front_rh) > k["lower_above_mm"]:
            done += b.add("platform", ["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"], SEMANTIC["down"]("WM_RIDEHEIGHT-W_FL", b.scales),
                          1, f"The front never gets below {min(front_rh):.1f} mm and barely touches down → lower it a step for downforce",
                          quiet=True)


def _packers_in(b: _Builder, s: dict, k: dict, axle: str, why: str) -> bool:
    """One more step of third packer on an axle that bottoms but hardly uses them."""
    frac = s.get(f"third_packers_{axle[0]}")
    key = f"VM_{axle.upper()}_3RD_PACKERS"
    if frac is None or frac >= k["third_packers_lo"] or not b.usable(key):
        return False
    return b.add("platform", [key], SEMANTIC["more"](key, b.scales), 1,
                 f"{why} while its third spring is on the packers only {frac:.0%} of the lap "
                 f"→ one step more {axle} third packer", quiet=True)


def _balance(b: _Builder, s: dict, rating: dict, k: dict, w: dict, size: str, recent: set[str],
             targets: dict[str, Target], tolerance: dict[str, float], wing_effect: float = 0.0) -> None:
    """One move per zone (corner speed × phase) that has a problem, worst first, up to the
    cap.

    Evidence: your ratings first — a zone you rated, or (from before the zones) a whole
    corner phase or corner speed; half a point only teaches what you like (the optimiser
    moves towards it), a point or more is fixed directly. Otherwise telemetry, per corner
    phase countersteer beyond what you've shown you're fine with (``tolerance``, learned
    from your ratings: a loose car or a naturally loose corner isn't a problem if you like
    it, but it can be) for oversteer, then per zone the understeer angle against the one
    you like: a rating point or more above it is understeer, and once the target is yours
    (the zone rated twice, or a car feel picked), a point or more below it is too much
    rotation. Time on traction control isn't oversteer evidence: a higher TC map cuts in
    more by itself, so "TC busy → more TC" would only ever ratchet up. Instead, TC that's
    busy while the exit doesn't oversteer is turned down a step."""
    order = ZONES + PHASES + SPEEDS
    problems = []  # (severity, zone, 'under'|'over', evidence, source)
    for z in order:
        v = rating.get(z)
        if v is not None and abs(v) >= 1:
            problems.append((abs(v), z, "under" if v > 0 else "over",
                             f"you rated {PHASE_WORDS[z].lower()} {'understeer' if v > 0 else 'oversteer'} ({v:+g})",
                             "rating"))
    rated = set(zone_ratings(rating))  # zones you said something about, if only "neutral"
    flagged = set()
    for ph in PHASES:
        if any(zone_parts(z)[1] == ph for z in rated):
            continue
        cs = s.get(f"cs_{ph}") or 0.0
        tol = tolerance.get(ph, k["countersteer_per_lap"])
        if cs >= tol:
            usual = "" if tol == k["countersteer_per_lap"] else f" (you're fine up to about {tol - CS_MARGIN:.1f})"
            problems.append((2 if cs >= 2 * tol else 1, ph, "over",
                             f"{PHASE_WORDS[ph].lower()}: {cs:.1f} countersteer moments per lap{usual}", "telemetry"))
            flagged.add(ph)
    for z in ZONES:
        us, t = s.get(f"us_{z}"), targets.get(z)
        if z in rated or zone_parts(z)[1] in flagged or us is None or t is None:
            continue
        points = t.points(us)
        liked = f"you like about {t.angle:.2f}°" if t.n or t.chosen else f"usual up to {t.limit:g}°"
        if points >= 1:
            problems.append((2 if points >= 2 else 1, z, "under",
                             f"{PHASE_WORDS[z].lower()}: understeer angle {us:.2f}° ({liked})", "telemetry"))
        elif points <= -1 and (t.n >= 2 or t.chosen):
            problems.append((2 if points <= -2 else 1, z, "over",
                             f"{PHASE_WORDS[z].lower()}: understeer angle {us:.2f}°, more rotation than you like "
                             f"(about {t.angle:.2f}°)", "telemetry"))
    cap, mult = RULE_SIZE.get(size, RULE_SIZE["large"])
    if w["drivability"] < 0.3:
        cap = 1
    problems.sort(key=lambda p: (-p[0], order.index(p[1])))
    made = 0
    for sev, ph, kind, evidence, source in problems:
        if made >= cap:
            break
        actions = _zone_actions(ph, kind)
        actions = [a for a in actions if not set(a[0]) & recent] + [a for a in actions if set(a[0]) & recent]
        # The wing costs lap time one way on a drag track (more) or a downforce track (less):
        # there it's the last resort.
        costly = "up" if wing_effect >= WING_COSTLY_S else ("down" if wing_effect <= -WING_COSTLY_S else None)
        actions = ([a for a in actions if not (a[0] == ["VM_REAR_WING"] and a[1] == costly)]
                   + [a for a in actions if a[0] == ["VM_REAR_WING"] and a[1] == costly])
        for keys, intent, words in actions:
            if not all(b.usable(key) or key in b.locks for key in keys if key in b.setup.params):
                continue
            if b.add("balance", keys, SEMANTIC[intent](keys[0], b.scales), _steps_for(keys[0], sev, b.scales, mult),
                     f"{evidence[0].upper()}{evidence[1:]} → {words}"):
                b.changes[-1].source = source
                made += 1
                break
    tc = s.get("tc_s") or 0.0
    exit_over = any(kind == "over" and (ph == "exit" or ph.endswith("_exit")) for _, ph, kind, _, _ in problems)
    if tc >= k["tc_s_per_lap"] and not exit_over:
        tc_actions = [
            (["VM_TRACTIONCONTROLMAP"], "less", "less traction control"),
            (["VM_TRACTIONCONTROLSLIPANGLEMAP"], "less", "less restrictive TC slip (allow more rotation on power)"),
            (["VM_TRACTIONCONTROLPOWERCUTMAP"], "less", "less TC power cut (prevent engine bogging)"),
        ]
        tc_actions = [a for a in tc_actions if not set(a[0]) & recent] + [a for a in tc_actions if set(a[0]) & recent]
        for keys, intent, words in tc_actions:
            if not all(b.usable(k) for k in keys if k in b.setup.params):
                continue
            if b.add("balance", keys, SEMANTIC[intent](keys[0], b.scales), 1,
                     f"Traction control cuts in {tc:.1f} s per lap without exit oversteer → {words}"):
                b.changes[-1].source = "telemetry"
                break
