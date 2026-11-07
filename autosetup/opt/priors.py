"""What each main setting does to the balance: a known direction at a fixed size.

Per setting, the cells (phase × speed band) it moves and how strongly, for going the way
the table says (stiffer / up / more / front); + is more understeer. The size is per real
step (a click, or 1 mm / 0.1° / 10 Nm / 1 % where the labels have that unit): one step
moves the setting's strongest cell by its ``step_deg_<kind>``, so a car with more clicks
doesn't get a smaller effect per click. Without a step size, or a unit its labels don't
show, a weight of 1 across the whole range is ``prior_full_range_deg``. These aren't learned from your
stints: which way a bar pushes is physics, and what one track's stints say about its size
doesn't carry to another track's corners. Only where the car sits at each track is
measured (balance_model.py). Wing and ride heights get their high-speed effect from the
car's aero map once it knows it.

Directions follow each car's labels (a ride-height click up raises the car if its label
grows with the index), like the rules do.

The braking cells are the braking zone before the car starts cornering: + is a car
that stays straight, − one whose rear moves around (stepping out, needing correction).

Settings that only work while a pedal is in a given state (USAGE) are sized by how you
use that pedal in each cell, measured from your own laps: brake bias works wherever you
are on the brake, the coast ramp wherever you're off the throttle, the power ramp
wherever you're on it. Trail the brake to the apex and brake bias moves your mid-corner;
come off it before turning and it doesn't.
"""

from __future__ import annotations

from autosetup.feel.measure import CELLS
from autosetup.opt.space import Dim


def _cells(**w: float) -> dict[str, float]:
    """Weights by cell; ``low=`` / ``high=`` / ``all=`` fill a band or every cornering cell
    (the braking cells only take what's named for them)."""
    out = {c: (0.0 if c.startswith("braking_") else w.get("all", 0.0)) for c in CELLS}
    for c in CELLS:
        band = c.split("_")[1]
        if band in w and not c.startswith("braking_"):
            out[c] = w[band]
    for c, v in w.items():
        if c in out:
            out[c] = v
    return {c: v for c, v in out.items() if v}


PRIORS: dict[str, tuple[str, dict[str, float]]] = {
    # More rear wing: rear downforce, understeer where downforce matters, and a steadier
    # rear under braking from speed.
    "wing": ("up", _cells(high=1.0, entry_high=0.5, low=0.2, entry_low=0.1, braking_high=0.5, braking_low=0.1)),
    # Raising the rear (more rake) brings the aero balance forward and loads the front.
    # More front wing: front downforce, less understeer where downforce matters.
    "front_wing": ("up", _cells(high=-1.0, entry_high=-0.5, low=-0.2, entry_low=-0.1)),
    "rh_rear": ("up", _cells(low=-0.6, high=-1.0, braking_low=-0.3, braking_high=-0.5)),
    "rh_front": ("up", _cells(low=0.6, high=1.0, braking_low=0.3, braking_high=0.5)),
    # Roll stiffness at an end takes grip from that end, most in slow corners.
    "arb_front": ("stiffer", _cells(entry_low=0.5, mid_low=1.0, exit_low=1.0, entry_high=0.3, mid_high=0.6,
                                    exit_high=0.6)),
    "arb_rear": ("stiffer", _cells(entry_low=-0.5, mid_low=-1.0, exit_low=-1.0, entry_high=-0.3, mid_high=-0.6,
                                   exit_high=-0.6)),
    "spring_front": ("stiffer", _cells(entry_low=0.4, mid_low=0.7, exit_low=0.7, entry_high=0.3, mid_high=0.5,
                                       exit_high=0.5)),
    "spring_rear": ("stiffer", _cells(entry_low=-0.4, mid_low=-0.7, exit_low=-0.7, entry_high=-0.3, mid_high=-0.5,
                                      exit_high=-0.5)),
    # A third spring holds the car up at speed: a stiffer front third keeps the front higher
    # in fast corners, so less front downforce there.
    "third_spring_front": ("stiffer", _cells(high=0.6, entry_high=0.4, low=0.1)),
    "third_spring_rear": ("stiffer", _cells(high=-0.6, entry_high=-0.4, low=-0.1)),
    # Preload locks the rear axle: it resists rotation on entry and pushes on exit.
    "diff_preload": ("more", _cells(entry_low=0.7, mid_low=0.3, exit_low=0.8, entry_high=0.3, mid_high=0.1,
                                    exit_high=0.4, braking_low=0.3, braking_high=0.2)),
    # Diff ramps, as more locking: on the coast it steadies the rear off throttle (understeer
    # at turn-in), on power it pushes on exit. The front axle drives only part of the time,
    # so the front diff does less.
    "diff_coast": ("locking", _cells(entry_low=0.6, entry_high=0.4, mid_low=0.2, braking_low=0.5, braking_high=0.3)),
    "diff_power": ("locking", _cells(exit_low=0.7, exit_high=0.4, mid_low=0.2)),
    "front_diff_power": ("locking", _cells(exit_low=0.4, exit_high=0.3)),
    "front_diff_coast": ("locking", _cells(entry_low=0.3, entry_high=0.2)),
    "front_diff_preload": ("locking", _cells(entry_low=0.2, exit_low=0.3)),
    # Toe-out at the front sharpens turn-in; toe-in at the rear steadies the car on power
    # and through long corners. Labels grow with toe-in.
    "toe_front": ("up", _cells(entry_low=0.6, entry_high=0.4, mid_low=0.2, mid_high=0.1)),
    "toe_rear": ("up", _cells(entry_low=0.2, entry_high=0.2, mid_low=0.5, mid_high=0.4, exit_low=0.7,
                              exit_high=0.5)),
    # A tender spring on (Off → Standard) softens the axle's first travel: like a softer
    # spring there, a little.
    "tender_front": ("more", _cells(entry_low=-0.2, mid_low=-0.3, exit_low=-0.3, entry_high=-0.1, mid_high=-0.2,
                                    exit_high=-0.2)),
    "tender_rear": ("more", _cells(entry_low=0.2, mid_low=0.3, exit_low=0.3, entry_high=0.1, mid_high=0.2,
                                   exit_high=0.2)),
    # Brake bias forward: the fronts do more of the braking, the rear stays planted on entry.
    "brake_bias": ("front", _cells(entry_low=1.0, entry_high=0.8, braking_low=1.0, braking_high=1.0)),
    # Slow dampers shape each phase's weight transfer: on the brakes the front is in bump and
    # the rear in rebound, on power the other way round. Damping an end harder moves load
    # across it sooner, which costs that end grip in the transition.
    "slow_bump_front": ("stiffer", _cells(entry_low=0.3, entry_high=0.2, braking_low=0.2, braking_high=0.2)),
    "slow_rebound_rear": ("stiffer", _cells(entry_low=-0.3, entry_high=-0.2, braking_low=-0.2, braking_high=-0.2)),
    "slow_rebound_front": ("stiffer", _cells(exit_low=-0.3, exit_high=-0.2)),
    "slow_bump_rear": ("stiffer", _cells(exit_low=0.3, exit_high=0.2)),
}


# Settings that work only in one pedal state: (what to read from the cell's usage, the use
# at which the effect is full, the effect by band at full use). With usage measured, these
# replace the PRIORS weights above (kept for when it isn't).
USAGE: dict[str, tuple[str, float, dict[str, float]]] = {
    "brake_bias": ("brake", 0.5, {"low": 1.0, "high": 1.0}),
    "diff_coast": ("off_throttle", 0.8, {"low": 0.6, "high": 0.4}),
    "front_diff_coast": ("off_throttle", 0.8, {"low": 0.3, "high": 0.2}),
    "diff_power": ("throttle", 0.7, {"low": 0.7, "high": 0.4}),
    "front_diff_power": ("throttle", 0.7, {"low": 0.4, "high": 0.3}),
}


def usage_weights(name: str, usage: dict[str, dict[str, float]] | None) -> dict[str, float] | None:
    """A USAGE setting's weight per cell from how you use the pedal there; None without usage."""
    if name not in USAGE or not usage:
        return None
    what, full, by_band = USAGE[name]
    out = {}
    for c in CELLS:
        u = usage.get(c)
        if u is None:
            continue
        use = 1.0 - u.get("throttle", 0.0) if what == "off_throttle" else u.get(what, 0.0)
        w = by_band[c.split("_")[1]] * min(1.0, max(0.0, use) / full)
        if w > 1e-3:
            out[c] = w
    return out


def index_direction(intent: str, dim: Dim, scales=None) -> int:
    """+1 if a higher index goes the way of ``intent``. 'stiffer' / 'more' are the game's
    index convention; 'up' / 'front' follow the label's number (CarScales.per_step);
    'locking' too, except a diff ramp angle, which locks more the lower it is."""
    if intent in ("stiffer", "more"):
        return 1
    if scales is None:
        return 1
    sc = scales[dim.keys[0]]
    step = sc.per_step
    up = 1 if step is None or step > 0 else -1
    if intent == "locking" and any("deg" in label for label in sc.points.values()):
        return -up  # a ramp angle: the lower, the more it locks
    return up  # 'up' / 'front', and a locking % (the higher, the more it locks)


# Step kind per setting, and the label units in one step with the text that marks the unit
# in a label (None: a click).
KINDS: dict[str, tuple[str, float | None, str | None]] = {
    "arb_front": ("arb", None, None), "arb_rear": ("arb", None, None),
    "spring_front": ("spring", None, None), "spring_rear": ("spring", None, None),
    "third_spring_front": ("third_spring", None, None), "third_spring_rear": ("third_spring", None, None),
    "tender_front": ("tender", None, None), "tender_rear": ("tender", None, None),
    "wing": ("wing", None, None), "front_wing": ("wing", None, None),
    "rh_front": ("ride_height", 0.1, "cm"), "rh_rear": ("ride_height", 0.1, "cm"),  # 1 mm
    "toe_front": ("toe", 0.1, "deg"), "toe_rear": ("toe", 0.1, "deg"),
    "diff_preload": ("preload", 10.0, "Nm"), "front_diff_preload": ("preload", 10.0, "Nm"),
    "brake_bias": ("brake_bias", 1.0, ":"),  # front %, labels 'front:rear'
    "slow_bump_front": ("damper", None, None), "slow_rebound_front": ("damper", None, None),
    "slow_bump_rear": ("damper", None, None), "slow_rebound_rear": ("damper", None, None),
    "diff_power": ("diff_ramp", None, None), "diff_coast": ("diff_ramp", None, None),
    "front_diff_power": ("diff_ramp", None, None), "front_diff_coast": ("diff_ramp", None, None),
}


def steps_per_click(d: Dim, scales=None) -> float | None:
    """Real steps in one click of ``d``; None when its labels don't tell the unit (a preload
    labelled '3' is a click, not 3 Nm)."""
    kind = KINDS.get(d.name)
    if kind is None:
        return None
    _, size, unit = kind
    if size is None:
        return 1.0
    if scales is None:
        return None
    sc = scales[d.keys[0]]
    if not any(unit in label for label in sc.points.values()):
        return None
    return abs(sc.per_step) / size if sc.per_step else None


def sensitivities(dims: list[Dim], knowledge: dict, scales=None,
                  usage: dict[str, dict[str, float]] | None = None) -> dict[str, dict[str, float]]:
    """Per dim name, per cell: deg of understeer per unit of the dim's range (0..1), going
    up in index. ``usage``: per cell, how you use the pedals (feel/measure.py), which
    sizes the USAGE settings."""
    full = float(knowledge["prior_full_range_deg"])
    out = {}
    for d in dims:
        if d.name not in PRIORS:
            continue
        intent, weights = PRIORS[d.name]
        top = max(abs(w) for w in weights.values())
        used = usage_weights(d.name, usage)
        if used is not None:
            weights, top = used, max(USAGE[d.name][2].values())  # sized at full use, not rescaled
        sign = index_direction(intent, d, scales)
        size = knowledge.get(f"step_deg_{KINDS[d.name][0]}") if d.name in KINDS else None
        per_click = steps_per_click(d, scales)
        if size is None or per_click is None:
            out[d.name] = {c: sign * w * full for c, w in weights.items()}
            continue
        lo, hi = d.span or (d.lo, d.hi)
        out[d.name] = {c: sign * w / top * float(size) * per_click * (hi - lo) for c, w in weights.items()}
    return out
