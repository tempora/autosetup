"""Learning the trade-off from what you do instead of from sliders.

How much pace, tyre wear and drivability matter to you is a weight vector on the
simplex. It's learned from pairwise evidence between stints driven on different setups:

  * revert  you tried a new setup and went back to one you had before: the one you
            went back to beat the one you left (weight 1);
  * keep    you moved on from a new setup to something built on it rather than going
            back: weak evidence the new setup beat the one before it (weight 0.3);
  * rating  two rated stints of the same car + track: the higher rating wins
            (weight |difference| / 2, at most 1).

Each piece of evidence compares the stints' objectives (corrected pace, wear, the
drivability cost) standardised by how much a setup plausibly moves them. A
Bradley–Terry likelihood, P(a beats b) = σ(β · w·(z_b − z_a)), is evaluated on a grid
over the simplex with a Dirichlet prior centred on the old defaults, so a handful of
comparisons nudge the weights and many move them. Preference is the driver's, not the
car's: evidence from every car and track is pooled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from autosetup.lmu.setup_space import CarSetup
from autosetup.opt.dataset import OBJECTIVES, Dataset, build_dataset
from autosetup.opt.optimizer import prior_spread
from autosetup.store import Store

DEFAULT_WEIGHTS = {"pace": 1.0, "tyres": 0.5, "drivability": 0.75}
PRIOR_STRENGTH = 12.0  # Dirichlet concentration: ~a dozen comparisons to move far
BETA = 2.0  # how decisively a standardised difference of 1 settles a comparison
GRID_STEPS = 20  # simplex grid at 5 % resolution
KEEP_WEIGHT = 0.3
# Settings that don't describe how the car handles (strategy, cooling, a new tyre set).
NOT_HANDLING = ("FUEL", "VIRTUAL_ENERGY", "COMPOUND", "PITSTOP", "RADIATOR", "ENGINE_MIXTURE")


@dataclass
class Comparison:
    winner: int  # stint ids
    loser: int
    source: str  # revert | keep | rating
    weight: float
    dz: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(3))  # z_loser - z_winner


@dataclass
class Preference:
    weights: dict[str, float]  # scaled so the largest is 1 (what the rules expect)
    simplex: dict[str, float]  # the same, summing to 1
    sd: dict[str, float]  # posterior sd on the simplex
    comparisons: list[Comparison]

    def to_json(self) -> dict:
        by_source: dict[str, int] = {}
        for c in self.comparisons:
            by_source[c.source] = by_source.get(c.source, 0) + 1
        return {"weights": self.weights, "simplex": self.simplex, "sd": self.sd,
                "comparisons": len(self.comparisons), "by_source": by_source,
                "prior": _normalise(DEFAULT_WEIGHTS),
                "evidence": [dict(winner=c.winner, loser=c.loser, source=c.source, weight=c.weight)
                             for c in self.comparisons]}


def _normalise(w: dict[str, float]) -> dict[str, float]:
    s = sum(w.values())
    return {k: v / s for k, v in w.items()}


def _grid() -> np.ndarray:
    n = GRID_STEPS
    pts = [(i, j, n - i - j) for i in range(1, n) for j in range(1, n - i)]  # interior only
    return np.array(pts, dtype=float) / n


def learn(comparisons: list[Comparison]) -> Preference:
    grid = _grid()
    w0 = np.array([_normalise(DEFAULT_WEIGHTS)[k] for k in OBJECTIVES])
    logp = ((PRIOR_STRENGTH * w0 - 1) * np.log(grid)).sum(1)
    for c in comparisons:
        margin = BETA * grid @ c.dz
        logp += c.weight * -np.logaddexp(0.0, -margin)  # log σ(margin)
    p = np.exp(logp - logp.max())
    p /= p.sum()
    mean = p @ grid
    sd = np.sqrt(np.maximum(p @ grid ** 2 - mean ** 2, 0.0))
    simplex = {k: float(v) for k, v in zip(OBJECTIVES, mean)}
    top = max(simplex.values())
    return Preference({k: v / top for k, v in simplex.items()}, simplex,
                      {k: float(v) for k, v in zip(OBJECTIVES, sd)}, comparisons)


# -- evidence -----------------------------------------------------------------------------
def fingerprint(setup: CarSetup) -> tuple:
    return tuple(sorted((k, p.value) for k, p in setup.params.items()
                        if p.adjustable and not any(s in k for s in NOT_HANDLING)))


def _distance(a: tuple, b: tuple) -> int:
    da, db = dict(a), dict(b)
    return sum(da.get(k) != db.get(k) for k in set(da) | set(db))


def evidence(store: Store, fuel_for_class) -> list[Comparison]:
    """Comparisons from every car + track the store knows. ``fuel_for_class``: car
    class → seconds per litre (the knowledge file), for the pace correction."""
    out: list[Comparison] = []
    domains = store.con.execute(
        """select car_signature, coalesce(track_layout, track), any_value(car_class) from recordings
           where duration > 30 and car_signature is not null group by all""").fetchall()
    for sig, layout, car_class in domains:
        ds = build_dataset(store, f"{sig}|{layout}", float(fuel_for_class(car_class or "")), transfer=False)
        if len(ds.obs) < 2:
            continue
        out += _domain_evidence(store, ds, sig, layout)
    return out


def _domain_evidence(store: Store, ds: Dataset, sig: str, layout: str) -> list[Comparison]:
    spread = np.array([prior_spread(n, ds) for n in OBJECTIVES])
    z = {o.stint_id: np.array([o.y[n] for n in OBJECTIVES]) / spread for o in ds.obs}
    rows = store.con.execute(
        """select id, file_name, setup_json from recordings
           where car_signature = ? and coalesce(track_layout, track) = ? and duration > 30
           order by file_mtime""", [sig, layout]).fetchall()

    # Runs: consecutive stints on the same handling setup, represented by their stints'
    # mean objectives (only stints with enough clean laps have any).
    runs: list[dict] = []
    for rid, file_name, setup_json in rows:
        fp = fingerprint(CarSetup.from_json(json.loads(setup_json)))
        if runs and runs[-1]["fp"] == fp:
            runs[-1]["ids"].append(rid)
        else:
            runs.append({"fp": fp, "ids": [rid]})
    for r in runs:
        zs = [z[i] for i in r["ids"] if i in z]
        r["z"] = np.nanmean(zs, axis=0) if zs else None
        r["stint"] = next((i for i in reversed(r["ids"]) if i in z), None)

    out: list[Comparison] = []

    def add(win: dict, lose: dict, source: str, weight: float) -> None:
        if win["z"] is None or lose["z"] is None:
            return
        dz = np.nan_to_num(lose["z"] - win["z"])
        out.append(Comparison(win["stint"], lose["stint"], source, weight, dz))

    for i in range(1, len(runs) - 1):
        left, nxt = runs[i], runs[i + 1]
        back = next((runs[j] for j in range(i - 1, -1, -1) if runs[j]["fp"] == nxt["fp"]), None)
        if back is not None:
            add(back, left, "revert", 1.0)
        elif _distance(nxt["fp"], left["fp"]) < _distance(nxt["fp"], runs[i - 1]["fp"]):
            add(left, runs[i - 1], "keep", KEEP_WEIGHT)

    rated = []
    for o in ds.obs:
        r = store.rating(o.file_name) or {}
        if r.get("rating") is not None:
            rated.append((o.stint_id, r["rating"]))
    for a in range(len(rated)):
        for b in range(a + 1, len(rated)):
            (ia, ra), (ib, rb) = rated[a], rated[b]
            if ra == rb:
                continue
            (wi, wr), (li, lr) = ((ia, ra), (ib, rb)) if ra > rb else ((ib, rb), (ia, ra))
            out.append(Comparison(wi, li, "rating", min(1.0, (wr - lr) / 2), np.nan_to_num(z[li] - z[wi])))
    return out
