"""What each main setting does to the balance: a known direction at a fixed size.

Per setting, the cells (phase × speed band) it moves and how strongly, for going the way
the table says (stiffer / up / more / front); + is more understeer. Across a setting's
whole range a weight of 1 is ``prior_full_range_deg``. These aren't learned from your
stints: which way a bar pushes is physics, and what one track's stints say about its size
doesn't carry to another track's corners. Only where the car sits at each track is
measured (balance_model.py). Wing and ride heights get their high-speed effect from the
car's aero map once it knows it.

Directions follow each car's labels (a ride-height click up raises the car if its label
grows with the index), like the rules do.
"""

from __future__ import annotations

from autosetup.feel.measure import CELLS
from autosetup.opt.space import Dim


def _cells(**w: float) -> dict[str, float]:
    """Weights by cell; ``low=`` / ``high=`` / ``all=`` fill a band or every cell."""
    out = {c: w.get("all", 0.0) for c in CELLS}
    for c in CELLS:
        band = c.split("_")[1]
        if band in w:
            out[c] = w[band]
    for c, v in w.items():
        if c in out:
            out[c] = v
    return {c: v for c, v in out.items() if v}


PRIORS: dict[str, tuple[str, dict[str, float]]] = {
    # More rear wing: rear downforce, understeer where downforce matters.
    "wing": ("up", _cells(high=1.0, entry_high=0.5, low=0.2, entry_low=0.1)),
    # Raising the rear (more rake) brings the aero balance forward and loads the front.
    "rh_rear": ("up", _cells(low=-0.6, high=-1.0)),
    "rh_front": ("up", _cells(low=0.6, high=1.0)),
    # Roll stiffness at an end takes grip from that end, most in slow corners.
    "arb_front": ("stiffer", _cells(entry_low=0.5, mid_low=1.0, exit_low=1.0, entry_high=0.3, mid_high=0.6,
                                    exit_high=0.6)),
    "arb_rear": ("stiffer", _cells(entry_low=-0.5, mid_low=-1.0, exit_low=-1.0, entry_high=-0.3, mid_high=-0.6,
                                   exit_high=-0.6)),
    "spring_front": ("stiffer", _cells(entry_low=0.4, mid_low=0.7, exit_low=0.7, entry_high=0.3, mid_high=0.5,
                                       exit_high=0.5)),
    "spring_rear": ("stiffer", _cells(entry_low=-0.4, mid_low=-0.7, exit_low=-0.7, entry_high=-0.3, mid_high=-0.5,
                                      exit_high=-0.5)),
    # Preload locks the rear axle: it resists rotation on entry and pushes on exit.
    "diff_preload": ("more", _cells(entry_low=0.7, mid_low=0.3, exit_low=0.8, entry_high=0.3, mid_high=0.1,
                                    exit_high=0.4)),
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
    "brake_bias": ("front", _cells(entry_low=1.0, entry_high=0.8)),
    # Slow dampers shape each phase's weight transfer: on the brakes the front is in bump and
    # the rear in rebound, on power the other way round. Damping an end harder moves load
    # across it sooner, which costs that end grip in the transition.
    "slow_bump_front": ("stiffer", _cells(entry_low=0.3, entry_high=0.2)),
    "slow_rebound_rear": ("stiffer", _cells(entry_low=-0.3, entry_high=-0.2)),
    "slow_rebound_front": ("stiffer", _cells(exit_low=-0.3, exit_high=-0.2)),
    "slow_bump_rear": ("stiffer", _cells(exit_low=0.3, exit_high=0.2)),
}


def index_direction(intent: str, dim: Dim, scales=None) -> int:
    """+1 if a higher index goes the way of ``intent``. 'stiffer' / 'more' are the game's
    index convention; 'up' / 'front' follow the label's number (CarScales.per_step)."""
    if intent in ("stiffer", "more"):
        return 1
    if scales is None:
        return 1
    step = scales[dim.keys[0]].per_step
    return 1 if step is None or step > 0 else -1


def sensitivities(dims: list[Dim], knowledge: dict, scales=None) -> dict[str, dict[str, float]]:
    """Per dim name, per cell: deg of understeer per unit of the dim's range (0..1), going
    up in index."""
    full = float(knowledge["prior_full_range_deg"])
    out = {}
    for d in dims:
        if d.name not in PRIORS:
            continue
        intent, weights = PRIORS[d.name]
        sign = index_direction(intent, d, scales)
        out[d.name] = {c: sign * w * full for c, w in weights.items()}
    return out
