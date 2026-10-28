"""Toy-car benchmark: does the optimisation loop find a better setup, and does it keep
the driver's own improvement apart from the setup's?

The toy car has the balance settings of a real GT3 setup — bars, brake bias, diff, wing,
TC, springs and four dampers per axle — with known (made-up) effects on pace, tyre wear
and drivability (several settings matter little, as on a real car), a driver who gets
~0.8 s faster over their first stints, and realistic noise (0.25 s lap-to-lap, 8 clean
laps a stint). ``run()`` drives N stints with the same policy the dashboard uses and
reports the true weighted cost of the setup driven each stint.

``transfer()`` checks transfer between tracks: the car is first run at track A, then
taken to track B, where some optima sit elsewhere and the lap is 10 s longer. It
compares the first stints at B with and without A's stints in the model.

    python -m autosetup.opt.bench
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np

from autosetup.lmu.setup_space import CarSetup
from autosetup.opt.dataset import Dataset, Observation, build_dims
from autosetup.opt.optimizer import CHANGE_SIZES, next_radius, optimise, weights_vector

SETTINGS = {  # key: (steps, start index)
    "VM_FRONT_ANTISWAY": (10, 8),
    "VM_REAR_ANTISWAY": (10, 1),
    "VM_BRAKE_BALANCE": (41, 12),
    "VM_DIFF_PRELOAD": (21, 17),
    "VM_REAR_WING": (9, 8),
    "VM_TRACTIONCONTROLMAP": (13, 2),
    "VM_ANTILOCKBRAKESYSTEMMAP": (11, 5),
    "VM_TRACTIONCONTROLPOWERCUTMAP": (13, 6),
}
DAMPERS = {  # per-wheel settings, tied per axle: key base: (steps, start front, start rear)
    "WM_SPRING": (7, 5, 1),
    "WM_SLOWBUMP": (41, 35, 30),
    "WM_SLOWREBOUND": (41, 30, 10),
    "WM_FASTBUMP": (51, 20, 20),
    "WM_FASTREBOUND": (51, 20, 20),
}
AXLES = {"F": ("FL", "FR"), "R": ("RL", "RR")}
LAPS, LAP_SD, BASE_LAP = 8, 0.25, 100.0
WEIGHTS = {"pace": 1.0, "tyres": 0.5, "drivability": 0.75}


def start_values() -> dict[str, int]:
    values = {k: s for k, (_, s) in SETTINGS.items()}
    for base, (_, front, rear) in DAMPERS.items():
        for axle, start in (("F", front), ("R", rear)):
            for w in AXLES[axle]:
                values[f"{base}-W_{w}"] = start
    return values


def make_setup(values: dict[str, int]) -> CarSetup:
    steps = {k: n for k, (n, _) in SETTINGS.items()}
    steps |= {f"{b}-W_{w}": n for b, (n, _, _) in DAMPERS.items() for ws in AXLES.values() for w in ws}
    data = {k: {"available": True, "minValue": 0, "maxValue": n, "value": values[k], "stringValue": str(values[k])}
            for k, n in steps.items()}
    return CarSetup.from_json(data)


def fractions(setup: CarSetup) -> dict[str, float]:
    f = {k: setup[k].value / (n - 1) for k, (n, _) in SETTINGS.items()}
    for b, (n, _, _) in DAMPERS.items():
        for axle, ws in AXLES.items():
            f[f"{b}:{axle}"] = setup[f"{b}-W_{ws[0]}"].value / (n - 1)
    return f


# Track B: a longer lap, wants more rear wing and a stiffer front bar; otherwise the same car.
TRACKS = {"A": {"base": 0.0, "rw": 0.35, "fa": 0.3}, "B": {"base": 10.0, "rw": 0.5, "fa": 0.45}}


def truth(setup: CarSetup, track: str = "A") -> dict[str, float]:
    """Noise-free objectives of a setup for a fully adapted driver."""
    f = fractions(setup)
    fa, ra, bb, dp, rw, tc = (f[k] for k in list(SETTINGS)[:6])
    tr = TRACKS[track]
    pace = BASE_LAP + tr["base"] + 1.2 * (fa - tr["fa"]) ** 2 + 1.0 * (ra - 0.6) ** 2 + 0.8 * (bb - 0.5) ** 2 \
        + 0.5 * (dp - 0.4) ** 2 + 1.5 * (rw - tr["rw"]) ** 2 + 0.3 * (tc - 0.4) ** 2 + 0.6 * (fa - ra + 0.3) ** 2 \
        + 0.8 * (f["WM_SPRING:F"] - 0.4) ** 2 + 0.5 * (f["WM_SPRING:R"] - 0.5) ** 2 \
        + 0.6 * (f["WM_SLOWBUMP:F"] - 0.4) ** 2 + 0.4 * (f["WM_SLOWREBOUND:R"] - 0.6) ** 2 \
        + 0.1 * (f["WM_FASTBUMP:F"] - 0.5) ** 2  # the rest barely matter
    tyres = 0.45 + 0.12 * (1 - rw) + 0.08 * (ra - 0.2) ** 2 + 0.05 * (1 - tc) + 0.04 * f["WM_SLOWREBOUND:F"]
    drive = 0.18 + 0.25 * (ra - 0.4) ** 2 + 0.15 * (tc - 0.7) ** 2 + 0.1 * (fa - 0.5) ** 2 \
        + 0.1 * (f["WM_SLOWBUMP:R"] - 0.5) ** 2 + 0.08 * (f["WM_FASTREBOUND:R"] - 0.5) ** 2
    return {"pace": pace, "tyres": tyres, "drivability": drive}


def true_cost(setup: CarSetup, ref: dict[str, tuple[float, float]], track: str = "A") -> float:
    """Weighted cost on a fixed scale, comparable across runs (lower is better)."""
    t = truth(setup, track)
    w = weights_vector(WEIGHTS)
    z = np.array([(t[n] - ref[n][0]) / ref[n][1] for n in ("pace", "tyres", "drivability")])
    return float((w * z).max() + 0.05 * (w * z).sum())


def drive_stint(setup: CarSetup, experience: float, rng: np.random.Generator, stint_id: int,
                track: str = "A") -> Observation:
    t = truth(setup, track)
    learning = 0.8 * np.exp(-experience / 25.0)  # the driver is slower early on
    laps = t["pace"] + learning + rng.normal(0, LAP_SD, LAPS)
    spread = float(np.std(laps, ddof=1))
    y = {"pace": float(laps.mean()), "tyres": t["tyres"] + rng.normal(0, 0.01),
         "drivability": t["drivability"] + rng.normal(0, 0.03)}
    noise = {"pace": LAP_SD ** 2 / LAPS, "tyres": 0.01 ** 2, "drivability": 0.03 ** 2}
    return Observation(stint_id, f"toy{stint_id}", setup, y, noise, experience, 30.0, LAPS, track)


@dataclass
class RunResult:
    costs: list[float]  # true cost of the setup driven each stint
    best_cost: float
    start_cost: float
    final_setup: CarSetup
    obs: list[Observation]


def run(stints: int = 20, seed: int = 0, policy: str = "model", size: str = "medium", track: str = "A",
        extra: list[Observation] | None = None) -> RunResult:
    """``extra``: stints of the same car at another track, for the model to transfer from."""
    rng = np.random.default_rng(seed)
    setup = make_setup(start_values())
    ref = {n: (v, {"pace": 0.3, "tyres": 0.05, "drivability": 0.05}[n]) for n, v in truth(setup, track).items()}
    extra = list(extra or [])
    obs: list[Observation] = []
    experience, radius = 0.0, CHANGE_SIZES[size].radius
    costs = []
    best_obs_cost = None
    for i in range(stints):
        o = drive_stint(setup, experience, rng, i, track)
        obs.append(o)
        experience += LAPS
        costs.append(true_cost(setup, ref, track))
        ds = Dataset(f"toy|{track}", build_dims(setup), obs, 0.0, 0.0, LAP_SD, extra=extra)
        if policy == "stay":
            continue
        if policy == "random":
            d = ds.dims[rng.integers(len(ds.dims))]
            cur = d.index(d.x(setup))
            nxt = int(np.clip(cur + rng.choice([-1, 1]), d.lo, d.hi))
            setup = _apply(setup, {k: nxt for k in d.keys})
            continue
        ready = len(obs) >= 2 and len(ds.all_obs) >= 4 and ds.distinct_setups_all >= 3  # as proposals.py
        res = optimise(ds, setup, WEIGHTS, (experience, 30.0), size=size, radius=radius,
                       learning=not ready, seed=seed * 1000 + i)
        # Trust region: did the last move pay off (observed, as the real loop sees it)?
        cost_now = _observed_cost(o, ref)
        if best_obs_cost is not None:
            radius = next_radius(radius, cost_now < best_obs_cost)
        best_obs_cost = cost_now if best_obs_cost is None else min(best_obs_cost, cost_now)
        if res.moves:
            setup = _apply(setup, {k: m.after for m in res.moves for k in m.dim.keys})
    return RunResult(costs, min(costs), costs[0], setup, obs)


def _observed_cost(o: Observation, ref) -> float:
    w = weights_vector(WEIGHTS)
    z = np.array([(o.y[n] - ref[n][0]) / ref[n][1] for n in ("pace", "tyres", "drivability")])
    return float((w * z).max() + 0.05 * (w * z).sum())


def _apply(setup: CarSetup, values: dict[str, int]) -> CarSetup:
    new = copy.deepcopy(setup)
    for k, v in values.items():
        p = new.params[k]
        new.params[k] = type(p)(p.key, int(v), p.min, p.max, str(int(v)), p.available)
    return new


def transfer(seeds: int = 5, home: int = 12, away: int = 8) -> dict[str, list[float]]:
    """Mean true cost per stint at track B, without and with track A's stints."""
    out: dict[str, list[list[float]]] = {"alone": [], "transfer": []}
    for seed in range(seeds):
        a = run(home, seed, "model", "large", track="A")
        out["alone"].append(run(away, 100 + seed, "model", "large", track="B").costs)
        out["transfer"].append(run(away, 100 + seed, "model", "large", track="B", extra=a.obs).costs)
    return {k: list(np.mean(v, axis=0)) for k, v in out.items()}


def main(seeds: int = 5, stints: int = 20) -> None:
    runs = [("stay", "medium"), ("random", "medium")] + [("model", s) for s in CHANGE_SIZES]
    for policy, size in runs:
        results = [run(stints, seed, policy, size) for seed in range(seeds)]
        at = {n: np.mean([r.costs[min(n, stints) - 1] for r in results]) for n in (5, 10, stints)}
        label = policy if policy != "model" else f"model/{size}"
        print(f"{label:13s} after 5: {at[5]:6.2f}   after 10: {at[10]:6.2f}   after {stints}: {at[stints]:6.2f}"
              f"   (mean of {seeds} seeds; start 0, lower is better)")
    t = transfer(seeds)
    for k, costs in t.items():
        print(f"track B {k:9s} after 2: {costs[1]:6.2f}   after 4: {costs[3]:6.2f}   after 8: {costs[7]:6.2f}"
              f"   (12 stints at track A first)")


if __name__ == "__main__":
    main()
