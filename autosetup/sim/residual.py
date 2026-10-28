"""Correcting the sim with your real laps.

Sector residuals: per stint, real sector times (mean over its clean laps) minus the
simulated ones for that stint's car. A GP per sector over the setup (inputs in [0, 1],
chosen by the optimiser) learns what the point mass misses: mechanical grip, traction,
kerbs, how the car suits you. Predictions are sim + residual; the real lap is the
correction signal, never the objective by itself.

Speed traps: drag mostly shows in top speed. Each stint's measured top speeds against the
simulated ones, relative to the other stints at the layout (so the driver efficiency and
the shared drive curve cancel), correct that stint's drag.
"""

from __future__ import annotations

import numpy as np

from autosetup.opt.gp import GP
from autosetup.sim.lapsim import simulate
from autosetup.sim.model import LayoutModel, sector_times

SECTOR_NOISE_FLOOR = 0.03 ** 2  # s²: a stint's mean sector time is never more certain than this
PRIOR_SPREAD = 0.15  # s: how much a setup plausibly moves one sector beyond what the sim sees
TRAP_DK = 2e-6  # finite-difference step for d(trap speed)/dk


def residual_data(store, model: LayoutModel) -> tuple[list[int], np.ndarray, np.ndarray]:
    """(stint ids, residuals (stints × sectors) s, their variances)."""
    rids, res, var = [], [], []
    n_sec = len(model.track.sectors) + 1
    for s in model.stints:
        laps = sector_times(store, s.rid, model.track.sectors, s.clean)
        laps = [lap for lap in laps if len(lap) == n_sec]
        if not laps:
            continue
        real = np.array(laps)
        sim = np.array(model.simulate(model.stint_car(s.rid)).sectors)
        rids.append(s.rid)
        res.append(real.mean(0) - sim)
        spread = real.var(0, ddof=1) if len(real) > 1 else np.full(n_sec, 0.1 ** 2)
        var.append(np.maximum(spread / len(real), SECTOR_NOISE_FLOOR))
    return rids, np.array(res).reshape(len(rids), n_sec), np.array(var).reshape(len(rids), n_sec)


class SectorResidual:
    def __init__(self, gps: list[GP]):
        self.gps = gps

    @classmethod
    def fit(cls, X: np.ndarray, residuals: np.ndarray, variances: np.ndarray) -> "SectorResidual":
        return cls([GP.fit(X, residuals[:, j], variances[:, j], PRIOR_SPREAD) for j in range(residuals.shape[1])])

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Mean and variance per sector, (n, sectors)."""
        out = [gp.predict(X) for gp in self.gps]
        return np.column_stack([m for m, _ in out]), np.column_stack([v for _, v in out])

    def lap(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        m, v = self.predict(X)
        return m.sum(1), v.sum(1)


def _measured_traps(model: LayoutModel, rid: int) -> list[float | None]:
    s = next(x for x in model.stints if x.rid == rid)
    out = []
    for d in model.track.traps:
        vs = [v for seq, dd, v in s.traps if abs(dd - d) < 150.0 and seq in s.clean]
        out.append(float(np.mean(vs)) if vs else None)
    return out


def trap_corrections(model: LayoutModel) -> dict[int, tuple[float, float]]:
    """Per stint, the drag change (1/m, se) that would make its simulated top speeds match
    the measured ones, relative to the layout's other stints."""
    if not model.track.traps:
        return {}
    gaps, slopes, sds = {}, {}, {}
    for s in model.stints:
        measured = _measured_traps(model, s.rid)
        if s.profile is None or all(m is None for m in measured):
            continue
        car = model.stint_car(s.rid)
        sim = simulate(model.track, car).traps
        sim_up = simulate(model.track, car.perturb(d_drag=TRAP_DK)).traps
        pairs = [(m, a, b) for m, a, b in zip(measured, sim, sim_up) if m is not None]
        gaps[s.rid] = float(np.mean([m - a for m, a, _ in pairs]))
        slopes[s.rid] = float(np.mean([(b - a) / TRAP_DK for _, a, b in pairs]))  # m/s per 1/m, < 0
        vs = [v for seq, _, v in s.traps if seq in s.clean]
        sds[s.rid] = max(float(np.std(vs)) / np.sqrt(max(len(vs), 1)), 0.2)
    if len(gaps) < 2:
        return {}
    mean_gap = float(np.mean(list(gaps.values())))
    # Δv = slope · δk: a stint slower than simulated (gap below the others') has more drag.
    return {rid: ((gaps[rid] - mean_gap) / slopes[rid], sds[rid] / abs(slopes[rid]))
            for rid in gaps if slopes[rid] < 0}


def with_trap_drag(model: LayoutModel) -> LayoutModel:
    """The model with each stint's drag the precision-weighted combination of what its
    straights and its top speeds say."""
    corr = trap_corrections(model)
    drag = dict(model.drag)
    for rid, (dk_t, se_t) in corr.items():
        dk_s, se_s = drag.get(rid, (0.0, np.inf))
        both = model.drag.get(rid, (0.0, 0.0))[0] + dk_t
        w_s, w_t = 1 / max(se_s, 1e-9) ** 2, 1 / max(se_t, 1e-9) ** 2
        drag[rid] = ((w_s * dk_s + w_t * both) / (w_s + w_t), float(np.sqrt(1 / (w_s + w_t))))
    model.drag = drag
    return model
