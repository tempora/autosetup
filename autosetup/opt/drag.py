"""How much a track punishes drag: where the rear wing's effect on lap time starts.

A track where you're flat out for much of the lap rewards less wing (Spa, Monza, Le Mans);
a twisty one rewards more. Measured from your own laps: the share of the lap at full
throttle (median over complete laps of this car class at this layout) against the usual
share for the class: the median over the layouts you've driven it at, or the knowledge
file's ``full_throttle_ref`` until there are MIN_LAYOUTS of them. ``wing_s_per_full_throttle``
turns the difference into seconds across the wing's whole range. The optimiser starts
from that and your stints correct it, as balance.py does for rotation; the rules reach
for the wing last where it would cost lap time.
"""

from __future__ import annotations

import numpy as np

from autosetup.store import Store

MIN_LAYOUTS = 3


def full_throttle_shares(store: Store, car_class: str) -> dict[str, float]:
    """Per layout: median share of the lap at full throttle, over complete laps of the class."""
    rows = store.con.execute(
        """select coalesce(r.track_layout, r.track), median(m.value) from recordings r
           join laps l on l.recording_id = r.id and l.complete and not l.in_pits
           join lap_metrics m on m.recording_id = r.id and m.seq = l.seq and m.name = 'full_throttle_frac'
           where r.car_class = ? group by 1""", [car_class]).fetchall()
    return {layout: float(v) for layout, v in rows if v is not None}


def wing_effect(shares: dict[str, float], layout: str, knowledge: dict) -> float:
    """Seconds a lap at ``layout`` is expected to lose going from the least to the most
    rear wing (negative: to gain). 0 until the layout has a complete lap."""
    if layout not in shares:
        return 0.0
    ref = (float(np.median(list(shares.values()))) if len(shares) >= MIN_LAYOUTS
           else float(knowledge["full_throttle_ref"]))
    cap = float(knowledge["wing_prior_max_s"])
    return float(np.clip(float(knowledge["wing_s_per_full_throttle"]) * (shares[layout] - ref), -cap, cap))
