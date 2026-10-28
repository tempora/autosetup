"""Predicted balance of a setup at a layout: where the car sits there, plus the known
effect of each change (priors.py).

    cell(x) = offset_cell + Σ_d s_d,cell · x_d        (x_d: share of the dim's range)

The offset is measured: each stint at the layout gives measured cell − Σ s·x, and they're
pooled by precision; the stint you're on counts as it is, the others with BETWEEN_SD more
doubt (the slopes are only the known direction at a typical size), so the model starts
from what you just drove. At a layout without stints the offset comes from the car's other
layouts (a track changes where a car sits, so with TRACK_SD more doubt), or from the
fingerprint: it assumes the fingerprint's setup gives the fingerprint's balance here.
The slopes carry SLOPE_REL_SD of doubt in their size (their direction is fixed), so a
bigger move is a less certain one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from autosetup.feel.measure import CELLS
from autosetup.opt.space import Dim

TRACK_SD = 0.3  # deg: how differently a car sits at another track
BETWEEN_SD = 0.15  # deg: what the slopes don't explain between two stints at one layout
SLOPE_REL_SD = 0.5
MODEL_SD = 0.08  # deg: misfit of the linear model, always there


@dataclass
class Evidence:
    """One stint's measured balance on a setup."""

    layout: str
    x: dict[str, float]  # dim name → share of range
    cells: dict[str, tuple[float, float]]  # cell → (mean, se)
    current: bool = False  # the stint the proposal starts from


@dataclass
class BalanceModel:
    slopes: dict[str, dict[str, float]]  # dim → cell → deg per unit x
    offset: dict[str, tuple[float, float]]  # cell → (mean, var)
    source: str = "none"  # layout | other layouts | fingerprint | none
    stints: int = 0
    x_ref: dict[str, float] = field(default_factory=dict)  # where the offset was measured (for move size)

    def effect(self, x: dict[str, float], cell: str) -> float:
        return sum(self.slopes.get(d, {}).get(cell, 0.0) * v for d, v in x.items())

    def predict(self, x: dict[str, float]) -> dict[str, tuple[float, float]]:
        out = {}
        for c, (m, var) in self.offset.items():
            moved = sum(abs(self.slopes.get(d, {}).get(c, 0.0) * (v - self.x_ref.get(d, v))) for d, v in x.items())
            out[c] = (m + self.effect(x, c), var + MODEL_SD ** 2 + (SLOPE_REL_SD * moved) ** 2)
        return out

    @classmethod
    def fit(cls, slopes: dict[str, dict[str, float]], layout: str, evidence: list[Evidence],
            fingerprint: tuple[dict[str, float], dict[str, float]] | None = None) -> "BalanceModel":
        """``evidence``: stints of this car (any layout); ``fingerprint``: (its x, its cells)."""
        def offsets(ev: list[Evidence]) -> dict[str, tuple[float, float]]:
            out = {}
            for c in CELLS:
                rows = [(e.cells[c][0] - sum(slopes.get(d, {}).get(c, 0.0) * v for d, v in e.x.items()),
                         e.cells[c][1] ** 2 + MODEL_SD ** 2 + (0.0 if e.current else BETWEEN_SD ** 2))
                        for e in ev if c in e.cells]
                if rows:
                    w = np.array([1 / v for _, v in rows])
                    out[c] = (float(np.sum(w * np.array([m for m, _ in rows])) / w.sum()), float(1 / w.sum()))
            return out

        here = [e for e in evidence if e.layout == layout]
        if here:
            ref = next((e for e in here if e.current), here[-1])
            return cls(slopes, offsets(here), "layout", len(here), dict(ref.x))
        others = [e for e in evidence if e.layout != layout]
        if others:
            per_layout = [offsets([e for e in others if e.layout == lay]) for lay in sorted({e.layout for e in others})]
            off = {}
            for c in CELLS:
                vals = [o[c] for o in per_layout if c in o]
                if vals:
                    m = float(np.mean([v[0] for v in vals]))
                    spread = float(np.var([v[0] for v in vals])) if len(vals) > 1 else 0.0
                    off[c] = (m, float(np.mean([v[1] for v in vals])) + max(spread, TRACK_SD ** 2))
            return cls(slopes, off, "other layouts", len(others), dict(others[-1].x))
        if fingerprint is not None:
            fx, fcells = fingerprint
            off = {c: (v - sum(slopes.get(d, {}).get(c, 0.0) * fx.get(d, 0.0) for d in slopes), TRACK_SD ** 2)
                   for c, v in fcells.items()}
            return cls(slopes, off, "fingerprint", 0, dict(fx))
        return cls(slopes, {}, "none", 0)
