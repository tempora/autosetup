"""Quasi-steady-state lap simulation of a point mass, on envelopes measured from your laps.

The car is three curves against speed, per unit mass (m/s²):
  lat     lateral grip                     μ·g·(1 + c·v²): grip plus downforce
  drive   net acceleration at full throttle (engine through the gears, minus drag)
  brake   deceleration at the limit         (tyres, downforce and drag together)
Measured, not modelled: a geared car's drive force changes with every gear, and fitting
it with a constant power gave negative rolling resistance and a sim 4–16 s off the real
lap. A setup change moves the curves: Δk·v² of drag off drive and onto brake, Δc·v² of
downforce onto lat and brake (``Car.perturb``).

The driver uses a share η of the grip and braking limits: one number per car and layout,
fitted so the simulated laps match yours. It's shared by every stint there, so it can't
soak up what a setup did.
Combined on a friction ellipse: what cornering uses of the grip leaves the rest for
braking or driving. Three passes over the profile: steady cornering speed per 5 m, then
forwards under drive and backwards under braking, the lap closing on its slowest corner.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import cached_property

import numpy as np

G = 9.81
V_CAP = 120.0  # m/s
V_GRID = np.arange(0.0, V_CAP + 0.25, 0.5)


@dataclass(frozen=True)
class Car:
    lat: np.ndarray  # m/s² on V_GRID: the car's limit
    drive: np.ndarray
    brake: np.ndarray
    mu: float  # lateral grip at zero speed, g (scales a downforce change)
    mu_brake: float
    eta: float = 1.0  # share of the limits the driver uses (fitted per car and layout)

    def perturb(self, d_drag: float = 0.0, d_lift: float = 0.0) -> "Car":
        """Δk (1/m) more drag, Δc (1/(m/s)²) more downforce."""
        v2 = V_GRID ** 2
        return replace(self, lat=np.maximum(self.lat + self.mu * G * d_lift * v2, 0.1),
                       drive=self.drive - d_drag * v2,
                       brake=np.maximum(self.brake + d_drag * v2 + self.mu_brake * G * d_lift * v2, 0.1))

    def scaled(self, eta: float) -> "Car":
        return replace(self, eta=eta)

    @cached_property
    def lat_eff(self) -> np.ndarray:
        return self.eta * self.lat

    @cached_property
    def brake_eff(self) -> np.ndarray:
        return self.eta * self.brake

    def at(self, curve: np.ndarray, v: float) -> float:
        i = min(int(v * 2.0), len(V_GRID) - 2)
        f = v * 2.0 - i
        return float(curve[i] * (1 - f) + curve[i + 1] * f)

    @property
    def top_speed(self) -> float:
        below = np.flatnonzero(self.drive <= 0)
        return float(V_GRID[below[0]]) if len(below) else V_CAP


@dataclass(frozen=True)
class Track:
    step: float  # m
    kappa: np.ndarray  # 1/m, per step (sign ignored)
    sectors: tuple[float, ...] = ()  # boundary distances
    traps: tuple[float, ...] = field(default=())  # speed-trap distances (end of each straight)

    @property
    def length(self) -> float:
        return self.step * len(self.kappa)


@dataclass
class Result:
    time: float
    v: np.ndarray  # m/s per step
    t: np.ndarray  # s at each step's start
    sectors: list[float]  # sector times, s
    traps: list[float]  # top speed near each trap, m/s


def corner_speed(car: Car, kappa: np.ndarray) -> np.ndarray:
    """Highest v with v²·|κ| ≤ lat(v), per step, interpolated between the speed grid's
    points (a quantised limit would make lap time jump with tiny setup changes).

    Downforce grip can grow faster than v²·|κ|, so a fast corner may be possible above some
    speed band it isn't possible in; the car comes into it from the straight, so the limit
    is the top of what's possible, not the first speed that fails."""
    k = np.abs(kappa)
    cap = car.top_speed
    margin = car.lat_eff[None, :] - V_GRID[None, :] ** 2 * k[:, None]  # (steps, speeds)
    good = (margin >= 0) & (V_GRID[None, :] <= cap)
    n = len(V_GRID)
    last = n - 1 - np.argmax(good[:, ::-1], axis=1)  # highest good speed index
    any_good = good.any(axis=1)
    rows = np.arange(len(k))
    nxt = np.minimum(last + 1, n - 1)
    m0, m1 = margin[rows, last], margin[rows, nxt]
    frac = np.where((nxt > last) & (m1 < 0), np.clip(m0 / np.maximum(m0 - m1, 1e-12), 0.0, 1.0), 0.0)
    v = V_GRID[last] + frac * (V_GRID[1] - V_GRID[0])
    v = np.where(any_good, v, 1.0)
    return np.clip(np.minimum(v, cap), 1.0, None)


def _left(a_cap: float, a_used: float) -> float:
    r = a_used / a_cap if a_cap > 0 else 1.0
    return float(np.sqrt(max(0.0, 1.0 - r * r)))


def fit_eta(laps: list[tuple[Track, Car, float]], lo: float = 0.8, hi: float = 1.05) -> float:
    """The η (shared by these laps: one car at one layout) that best matches the real lap
    times, by golden-section search on the squared error."""
    def err(eta: float) -> float:
        return sum((simulate(t, c.scaled(eta)).time - real) ** 2 for t, c, real in laps)

    g = (np.sqrt(5) - 1) / 2
    a, b = lo, hi
    x1, x2 = b - g * (b - a), a + g * (b - a)
    f1, f2 = err(x1), err(x2)
    for _ in range(18):
        if f1 < f2:
            b, x2, f2 = x2, x1, f1
            x1 = b - g * (b - a)
            f1 = err(x1)
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a + g * (b - a)
            f2 = err(x2)
    return float((a + b) / 2)


def simulate(track: Track, car: Car) -> Result:
    k = np.abs(track.kappa)
    n = len(k)
    ds = track.step
    vc = corner_speed(car, k)
    start = int(np.argmin(vc))
    order = (np.arange(n) + start) % n
    kk, lim = k[order], vc[order]
    fwd = np.empty(n)
    fwd[0] = lim[0]
    for i in range(n - 1):
        v = fwd[i]
        a = car.at(car.drive, v)
        if a > 0:
            a *= _left(car.at(car.lat_eff, v), v * v * kk[i])
        fwd[i + 1] = min(np.sqrt(max(v * v + 2.0 * a * ds, 1.0)), lim[i + 1])
    bwd = np.empty(n)
    v0 = lim[0]
    a0 = car.at(car.brake_eff, v0) * _left(car.at(car.lat_eff, v0), v0 * v0 * kk[0])
    bwd[-1] = min(fwd[-1], np.sqrt(v0 * v0 + 2.0 * a0 * ds))
    for i in range(n - 1, 0, -1):
        v = bwd[i]
        a = car.at(car.brake_eff, v) * _left(car.at(car.lat_eff, v), v * v * kk[i])
        bwd[i - 1] = min(np.sqrt(v * v + 2.0 * a * ds), fwd[i - 1])
    v = np.empty(n)
    v[order] = np.minimum(fwd, bwd)
    dt = 2.0 * ds / (v + np.roll(v, -1))
    t = np.concatenate([[0.0], np.cumsum(dt)[:-1]])
    total = float(dt.sum())
    d = np.arange(n) * ds
    marks = [float(np.interp(b, d, t)) for b in track.sectors if 0 < b < track.length]
    bounds = [0.0, *marks, total]
    traps = []
    for x in track.traps:
        near = (d >= x - 100.0) & (d <= x + 100.0)
        traps.append(float(np.max(v[near])) if near.any() else float("nan"))
    return Result(total, v, t, [b - a for a, b in zip(bounds, bounds[1:])], traps)
