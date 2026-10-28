"""How much rotation you like: a target understeer angle per zone (each corner phase within
each corner speed: "slow_exit" = exits of low-speed corners), learned from your ratings and
the car feel you pick.

Every stint measures the understeer angle (deg, positive = understeer) in each zone. Your
ratings (+ understeer, − oversteer, in rating points, halves allowed) say where that sits
against what you like. One rating point is taken as ``rating_point_<zone>`` degrees
(knowledge file), so a rated zone implies a target:

    target = measured − rating × per_point

Ratings are pooled over every stint of the car class (the angle a car runs at depends
on the class far more than on the track) and shrunk three times: each zone towards its
corner phase (the same phase at the other speeds), the phase towards your general shift,
so "I like a pointy car" carries over to zones you haven't rated, and the general shift
towards the knowledge file. Without ratings the targets sit one point below the
knowledge file's usual limits, so the rules act where they always did. Ratings from
before the zones (a whole phase, or a whole corner speed) count for every zone they cover.

The car feel moves it from there (Balance.with_feel): Pointier/Lazier a point on entry and
mid corner at every speed, Looser/Safer a point on exits and a countersteer moment per lap.

The same ratings learn how loose you're fine with it: countersteer per lap per phase
(Balance.tolerance). A loose car you like, or a corner that's always a bit loose,
countersteers without being a problem; beyond what you've accepted, it is one.

What a setting does to the balance isn't learned from your stints (Linear): which way
it pushes is known physics, and what one track's stints say about its size doesn't
carry to another track's corners. Only where the car sits now is measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from autosetup.opt.dataset import PHASES, SPEEDS, ZONES, zone_parts

PRIOR_GENERAL = 2.0  # rated zones' worth of evidence before the general shift moves far
PRIOR_PHASE = 1.0  # ... before a corner phase leaves the general shift
PRIOR_ZONE = 1.0  # ... and before a zone leaves its phase
S_PER_POINT = 0.1  # drivability cost of one rating point off your liking, s (as a rating)
PRIOR_POINTS = 2.0  # a setting's full range moves its phases by about this many rating points
CS_MARGIN = 0.5  # countersteer moments per lap above the most you've accepted before it counts
BAND = 0.5  # rating points either side of a target that are all "the balance you like"
FEEL_ZONES = {"front": tuple(z for z in ZONES if zone_parts(z)[1] in ("entry", "mid")),  # Pointier/Lazier
              "rear": tuple(z for z in ZONES if zone_parts(z)[1] == "exit")}  # Looser/Safer
FEEL_CS = 1.0  # countersteer moments per lap a step of rear feel accepts more (Looser) or less (Safer)
MIN_CS = 1.0


@dataclass(frozen=True)
class Target:
    angle: float  # understeer angle you like, deg
    per_point: float  # deg per rating point
    n: int  # rated stints of this phase behind it
    limit: float  # the knowledge file's usual upper limit
    chosen: bool = False  # you picked a car feel: the target is yours, not a default

    def points(self, angle: float) -> float:
        """Rating this angle would get from you: + understeer, − oversteer."""
        return (angle - self.angle) / self.per_point


@dataclass
class Balance:
    targets: dict[str, Target]
    shift: float  # general shift in rating points (+: you like more understeer than usual)
    n: int  # rated phases behind it
    countersteer: list[tuple[str, float, float, str]] = field(default_factory=list)  # (phase, per lap, rating, layout)
    feel: dict[str, int] = field(default_factory=dict)  # "front"/"rear": −1 pointier/looser, +1 lazier/safer

    def tolerance(self, knowledge: dict, layout: str | None = None) -> dict[str, float]:
        """Countersteer moments per lap per phase you're fine with, learned from your ratings.

        Countersteer isn't oversteer by itself: a loose car you're comfortable with, or a
        corner that's always a bit loose, countersteers too; but it can be the problem.
        From the knowledge file's threshold, your rated stints in order: a phase you didn't
        rate as oversteer accepts its countersteer (the tolerance rises just above it); one
        you rated as oversteer above the threshold brings the tolerance down to its level.
        This layout's ratings come last, so a corner that's naturally loose here is judged
        by what you said here; other layouts of the class carry over."""
        default = float(knowledge["countersteer_per_lap"])
        tol = {ph: default for ph in PHASES}
        for ph, cs, rating, _ in sorted(self.countersteer, key=lambda r: r[3] == layout):  # stable: in order
            if rating >= 0:
                tol[ph] = max(tol[ph], cs + CS_MARGIN)
            elif default <= cs < tol[ph]:
                tol[ph] = cs
        rear = self.feel.get("rear")
        if rear:  # Looser accepts more countersteer, Safer less
            tol = {ph: max(MIN_CS, v - rear * FEEL_CS) for ph, v in tol.items()}
        return tol

    def with_feel(self, front: int | None = None, rear: int | None = None) -> "Balance":
        """The targets moved by the car feel you picked, a rating point a step on top of
        what your ratings taught: ``front`` −1 Pointier / +1 Lazier (entry, mid corner),
        ``rear`` −1 Looser / +1 Safer (exit, and countersteer you accept). None: not picked.
        A zone you picked a feel for is yours (Target.chosen): pointier than it counts too."""
        feel = {k: v for k, v in (("front", front), ("rear", rear)) if v is not None}
        if not feel:
            return self
        targets = dict(self.targets)
        for side, step in feel.items():
            for ph in FEEL_ZONES[side]:
                t = targets[ph]
                targets[ph] = replace(t, angle=t.angle + step * t.per_point, chosen=True)
        return replace(self, targets=targets, feel=feel)

    def to_json(self) -> dict:
        return {"shift": self.shift, "n": self.n, "feel": self.feel,
                "targets": {ph: {"angle": t.angle, "per_point": t.per_point, "n": t.n} for ph, t in self.targets.items()}}


def learn(rated: list[tuple[str, float, float]], knowledge: dict,
          countersteer: list[tuple[str, float, float, str]] | None = None) -> Balance:
    """``rated``: (zone, measured angle, your rating of that zone); ``countersteer``:
    (phase, countersteer moments per lap, your rating of that phase, layout), in order."""
    pp = {ph: float(knowledge[f"rating_point_{ph}"]) for ph in ZONES}
    limit = {ph: float(knowledge[f"understeer_{ph}"]) for ph in ZONES}
    default = {ph: limit[ph] - pp[ph] for ph in ZONES}
    # Each rated zone as "how many points away from the default you'd put neutral".
    implied = [(z, (us - r * pp[z] - default[z]) / pp[z]) for z, us, r in rated if z in pp]
    shift = sum(x for _, x in implied) / (len(implied) + PRIOR_GENERAL)
    phase_shift = {}
    for ph in PHASES:
        xs = [x for z, x in implied if zone_parts(z)[1] == ph]
        phase_shift[ph] = (PRIOR_PHASE * shift + sum(xs)) / (PRIOR_PHASE + len(xs))
    targets = {}
    for z in ZONES:
        xs = [x for p, x in implied if p == z]
        t = (PRIOR_ZONE * phase_shift[zone_parts(z)[1]] + sum(xs)) / (PRIOR_ZONE + len(xs))
        targets[z] = Target(default[z] + pp[z] * t, pp[z], len(xs), limit[z])
    return Balance(targets, shift, len(implied), list(countersteer or ()))


def feel_key(car_class: str, side: str) -> str:
    """Setting holding the car feel you picked for this class: ``side`` front or rear."""
    return f"feel_{side}:{car_class}"


def reset_key(car_class: str) -> str:
    """Setting holding when you last reset this class's rotation learning."""
    return f"rotation_reset:{car_class}"


def rated_phases(store, car_class: str) -> list[tuple[str, float, float]]:
    """(zone, measured angle, rating) for every rated zone of this car class, leaving
    out ratings given before you last reset its rotation learning."""
    return rated_stints(store, car_class)[0]


def zone_ratings(rating: dict) -> dict[str, float]:
    """A stint's rating per zone. Ratings from before the zones count for every zone they
    cover (a whole phase across the speeds, a whole corner speed across the phases); a
    zone you rated itself wins."""
    out: dict[str, float] = {}
    for z in ZONES:
        sp, ph = zone_parts(z)
        for v in (rating.get(z), rating.get(ph), rating.get(sp)):
            if v is not None:
                out[z] = float(v)
                break
    return out


def rated_stints(store, car_class: str) -> tuple[list[tuple[str, float, float]], list[tuple[str, float, float, str]]]:
    """Every rated zone of this car class since you last reset its rotation learning, in
    order: (zone, measured angle, rating) and (phase, countersteer per lap, rating, layout);
    a phase's countersteer counts with the mean of its zones' ratings."""
    from autosetup.analysis import summaries

    since = store.get_setting(reset_key(car_class))
    rows = store.con.execute(
        """select r.id, coalesce(r.track_layout, r.track), r.file_name
           from recordings r join ratings g on g.file_name = r.file_name
           where r.car_class = ? and (?::timestamp is null or g.rated_at > ?::timestamp)
           order by r.file_mtime""", [car_class, since, since]).fetchall()
    rated = [(rid, layout, zone_ratings(store.rating(name) or {})) for rid, layout, name in rows]
    rated = [r for r in rated if r[2]]
    summary_by = summaries(store, [r[0] for r in rated])
    angles, countersteer = [], []
    for rid, layout, zones in rated:
        summary = summary_by[rid]
        for z, v in zones.items():
            us = summary.get(f"us_{z}")
            if us is not None:
                angles.append((z, float(us), v))
        for ph in PHASES:
            vs = [v for z, v in zones.items() if zone_parts(z)[1] == ph]
            cs = summary.get(f"cs_{ph}")
            if vs and cs is not None:
                countersteer.append((ph, float(cs), float(np.mean(vs)), layout))
    return angles, countersteer


class Linear:
    """What the setup does to one zone's understeer angle: a Bayesian linear model.

    angle = offset per track + sum of slope × setting (share of its range) + context.
    Balance is close to linear in each setting and its direction is mostly known (a
    stiffer front bar understeers). Each slope's prior is that direction at about two
    rating points across the range; the optimiser holds it there with a tight prior sd, so
    the stints only measure the offset (where the car runs at this track), not the slopes.
    """

    def __init__(self, X: np.ndarray, y: np.ndarray, noise: np.ndarray, groups: np.ndarray | None,
                 prior_mean: np.ndarray, prior_sd: np.ndarray, misfit: float):
        n_groups = int(groups.max()) + 1 if groups is not None and len(groups) else 1
        self.n_groups = n_groups
        Phi = self._phi(X, groups if groups is not None else np.zeros(len(X), int))
        m0 = np.concatenate([np.full(n_groups, float(np.mean(y))), prior_mean])
        s0 = np.concatenate([np.full(n_groups, 10.0 * max(float(np.max(prior_sd)), 0.1)), prior_sd])
        r = noise + misfit ** 2
        A = np.diag(1.0 / s0 ** 2) + Phi.T @ (Phi / r[:, None])
        self.cov = np.linalg.inv(A)
        self.mean = self.cov @ (m0 / s0 ** 2 + Phi.T @ (y / r))

    def _phi(self, X: np.ndarray, groups: np.ndarray) -> np.ndarray:
        onehot = np.zeros((len(X), self.n_groups))
        onehot[np.arange(len(X)), groups] = 1.0
        return np.hstack([onehot, X])

    def predict(self, Xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Mean and variance of the angle at the track being tuned (group 0)."""
        Phi = self._phi(np.atleast_2d(Xs), np.zeros(len(np.atleast_2d(Xs)), int))
        return Phi @ self.mean, np.maximum(np.einsum("ij,jk,ik->i", Phi, self.cov, Phi), 1e-12)

    def slopes(self) -> np.ndarray:
        return self.mean[self.n_groups:]
