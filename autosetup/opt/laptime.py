"""Predicted lap time of a setup at a layout: the lap model on the setup's aero platform,
plus the residual GP of what the model misses.

The platform of a candidate: rear wing as a share of its range, and the ride heights at
speed, moved from the reference stint's by the static change (mm per click from the
labels). Mechanical settings don't move the point mass; the residual GP, over the main
settings, is where they show. The lap time's doubt is the residual GP's plus the aero
map's about the drag and downforce of a platform away from the ones you drove; beyond
what you've driven (aero.SPAN_MM, a wing click) the map is a guess, and so is the lap
time. With no lap model at the layout (no complete lap yet) the lap time is unknown and
only the balance decides.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from autosetup.opt.bo import x_of
from autosetup.opt.space import Dim, aero_wing
from autosetup.sim.aero import AeroMap, Platform
from autosetup.sim.model import LayoutModel
from autosetup.sim.residual import PRIOR_SPREAD, SectorResidual, residual_data

UNKNOWN_SD = 5.0  # s: "no idea" when there is no lap model
D_DRAG, D_LIFT = 2e-6, 2e-6  # finite-difference steps for lap time per unit drag / downforce


def lap_function(store, model: LayoutModel | None, aero: AeroMap | None, dims: list[Dim],
                 ref: dict[str, int], ref_platform: Platform | None, mm_per_click: dict[str, float]
                 ) -> tuple[Callable[[dict[str, int]], tuple[float, float]], dict]:
    """(indices → (mean s, sd s), info). ``ref``: the indices ``ref_platform`` was measured on."""
    if model is None or ref_platform is None:
        return (lambda idx: (0.0, UNKNOWN_SD)), {"model": None}
    names = [d.name for d in dims]
    residual, n = None, 0
    rids, res, var = residual_data(store, model)
    if len(rids) >= 2:
        setups = {s.rid: s.setup for s in model.stints}
        X = np.array([[d.x(d.index(setups[r])) for d in dims] for r in rids])
        residual, n = SectorResidual.fit(X, res, var), len(rids)
    mean_res = float(res.sum(1).mean()) if len(rids) else 0.0
    cache: dict[tuple, float] = {}
    wing = aero_wing(dims)

    def platform(idx: dict[str, int]) -> Platform:
        w = wing.x(idx[wing.name]) if wing is not None else ref_platform.wing
        f = ref_platform.rh_front + (idx.get("rh_front", ref.get("rh_front", 0)) - ref.get("rh_front", 0)) * mm_per_click.get("rh_front", 1.0)
        r = ref_platform.rh_rear + (idx.get("rh_rear", ref.get("rh_rear", 0)) - ref.get("rh_rear", 0)) * mm_per_click.get("rh_rear", 1.0)
        return Platform(w, f, r)

    # What the aero map doesn't know: lap time per unit drag and downforce at the reference
    # platform, so a platform far from what you drove carries the map's doubt about it.
    sens = None
    if aero is not None:
        car0 = model.car_at(aero, ref_platform)
        t0 = model.simulate(car0).time
        sens = ((model.simulate(car0.perturb(d_drag=D_DRAG)).time - t0) / D_DRAG,
                (model.simulate(car0.perturb(d_lift=D_LIFT)).time - t0) / D_LIFT)

    def aero_var(p: Platform) -> float:
        if sens is None:
            return 0.0
        dx = p.x() - ref_platform.x()
        return sens[0] ** 2 * aero.drag.effect(dx)[1] ** 2 + sens[1] ** 2 * aero.lift.effect(dx)[1] ** 2

    wing_step = 1.0 / max(wing.hi - wing.lo, 1) if wing is not None else 0.0

    def lap(idx: dict[str, int]) -> tuple[float, float]:
        p = platform(idx)
        key = (round(p.wing, 4), round(p.rh_front, 2), round(p.rh_rear, 2))
        if key not in cache:
            car = model.car_at(aero, p) if aero is not None else model.base.scaled(model.eta)
            outside = aero is not None and not aero.within(p, wing_step)
            cache[key] = (model.simulate(car).time, UNKNOWN_SD ** 2 if outside else aero_var(p))
        t, av = cache[key]
        if residual is not None:
            x = np.array([[x_of(dims, idx)[nm] for nm in names]])
            m, v = residual.lap(x)
            return t + float(m[0]), float(np.sqrt(v[0] + av))
        return t + mean_res, float(np.sqrt((PRIOR_SPREAD ** 2) * (len(model.track.sectors) + 1) + av))

    return lap, {"model": model, "residual_stints": n}
