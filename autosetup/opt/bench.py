"""Toy-car benchmark: does the loop reach the balance you asked for, get faster inside it,
and carry a fingerprint to a new track?

The toy car has the main settings of a GT3 (rear wing, ride heights, bars, diff preload,
brake bias). Its true balance follows the priors' directions at sizes they don't know (each
slope 0.5–1.5× the prior), around a level that differs by track. Its true lap time has a
wing optimum, a gain from rake and a mechanical optimum the lap model doesn't see; each run
measures the balance with 0.08° and the lap with 0.1 s of noise. The loop is the app's:
balance model + residual GP on a biased lap model + constrained search, one setup a run.

    python -m autosetup.opt.bench
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from autosetup.feel.measure import CELLS
from autosetup.opt import bo
from autosetup.opt.balance_model import BalanceModel, Evidence
from autosetup.opt.gp import GP
from autosetup.opt.priors import sensitivities
from autosetup.opt.space import Dim

DIMS = [Dim("wing", ("VM_REAR_WING",), 0, 8), Dim("rh_rear", ("R",), 0, 30), Dim("rh_front", ("F",), 0, 30),
        Dim("arb_front", ("AF",), 0, 7), Dim("arb_rear", ("AR",), 0, 7), Dim("diff_preload", ("DP",), 0, 20),
        Dim("brake_bias", ("BB",), 0, 40)]
KNOWLEDGE = {"prior_full_range_deg": 0.6}
TOL = 0.15
STEP = 0.25
BAL_NOISE, LAP_NOISE = 0.08, 0.1


@dataclass
class World:
    slopes: dict[str, dict[str, float]]  # the truth: priors × a factor per setting
    level: dict[str, float]  # balance level per cell at this track
    seed: int

    def balance(self, idx: dict[str, int]) -> dict[str, float]:
        x = bo.x_of(DIMS, idx)
        return {c: self.level[c] + sum(self.slopes.get(d, {}).get(c, 0.0) * v for d, v in x.items()) for c in CELLS}

    def lap(self, idx: dict[str, int]) -> float:
        x = bo.x_of(DIMS, idx)
        return (100.0 + 1.2 * (x["wing"] - 0.6) ** 2 - 0.6 * x["rh_rear"] + 0.3 * x["rh_front"]
                + 0.4 * (x["arb_rear"] - 0.5) ** 2 + 0.3 * (x["diff_preload"] - 0.4) ** 2)


def world(seed: int, level_shift: float = 0.0) -> World:
    rng = np.random.default_rng(seed)
    prior = sensitivities(DIMS, KNOWLEDGE)
    slopes = {d: {c: v * rng.uniform(0.5, 1.5) for c, v in cells.items()} for d, cells in prior.items()}
    level = {c: 0.6 + level_shift + rng.normal(0, 0.1) for c in CELLS}
    return World(slopes, level, seed)


def model_lap(idx: dict[str, int]) -> float:
    """The lap model: right about wing and rake, a bit off, blind to the mechanical side."""
    x = bo.x_of(DIMS, idx)
    return 100.0 + 1.0 * (x["wing"] - 0.5) ** 2 - 0.5 * x["rh_rear"] + 0.3 * x["rh_front"]


def run(w: World, target: dict[str, float], start: dict[str, int], runs: int = 8, seed: int = 0,
        evidence: list[Evidence] | None = None, layout: str = "B", fingerprint=None) -> list[dict]:
    """Drive ``runs`` stints from ``start``; per stint: indices, true balance off the band
    (sum over cells, deg), true lap time."""
    rng = np.random.default_rng(seed)
    slopes = sensitivities(DIMS, KNOWLEDGE)
    evidence = list(evidence or [])
    laps: list[tuple[dict[str, int], float]] = []
    idx = dict(start)
    out = []
    band = bo.TargetBand(target, {c: TOL for c in target}, {c: 0.5 for c in target}, STEP)
    for _ in range(runs):
        true = w.balance(idx)
        out.append({"idx": dict(idx), "off": float(sum(max(0.0, abs(true[c] - target[c]) - TOL) for c in target)),
                    "lap": w.lap(idx)})
        evidence = [e for e in evidence if not (e.layout == layout and e.current)] + \
            [Evidence(e.layout, e.x, e.cells, False) for e in evidence if e.layout == layout and e.current]
        evidence.append(Evidence(layout, bo.x_of(DIMS, idx),
                                 {c: (true[c] + rng.normal(0, BAL_NOISE), BAL_NOISE) for c in CELLS}, True))
        laps.append((dict(idx), w.lap(idx) + rng.normal(0, LAP_NOISE)))
        balance = BalanceModel.fit(slopes, layout, evidence, fingerprint)
        X = np.array([[bo.x_of(DIMS, i)[d.name] for d in DIMS] for i, _ in laps])
        res = np.array([t - model_lap(i) for i, t in laps])
        gp = GP.fit(X, res, np.full(len(res), LAP_NOISE ** 2), 0.3) if len(laps) >= 2 else None

        def lap(i: dict[str, int]) -> tuple[float, float]:
            if gp is None:
                return model_lap(i) + (res.mean() if len(res) else 0.0), 0.3
            m, v = gp.predict(np.array([[bo.x_of(DIMS, i)[d.name] for d in DIMS]]))
            return model_lap(i) + float(m[0]), float(np.sqrt(v[0]))

        choice = bo.solve(bo.Problem(DIMS, idx, lap, balance, band, seed=int(rng.integers(1 << 30))))
        idx = dict(choice.best.indices)
    return out


def main() -> None:
    for seed in range(4):
        w = world(seed)
        target = {c: v - 0.3 for c, v in w.balance({d.name: (d.lo + d.hi) // 2 for d in DIMS}).items()}
        hist = run(w, target, {d.name: (d.lo + d.hi) // 2 for d in DIMS}, seed=seed)
        print(f"seed {seed}: off band {[round(h['off'], 2) for h in hist]}  lap {[round(h['lap'], 2) for h in hist]}")


if __name__ == "__main__":
    main()
