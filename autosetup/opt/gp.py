"""A small exact Gaussian process: enough for tens of stints, no heavy dependencies.

Inputs live in [0, 1] per dimension. The kernel is an RBF with one shared lengthscale
plus a constant mean; each observation carries its own known noise variance (a stint
with 15 clean laps is trusted more than one with 3). Hyperparameters are chosen on a
small grid by marginal likelihood plus weak priors: effects are expected to be smooth
(lengthscale around 0.8 of a setting's range) and of the size ``prior_spread`` says.
Without those priors two points can't pin anything down and the fit collapses to the
shortest lengthscale, where every stint looks unrelated to every other.

Several tracks: with ``groups`` (0 = the track being tuned, 1.. = other tracks of the
same car), the kernel is multiplied by rho + (1 - rho) * [same track]. A setting's
effect is then shared between tracks with correlation rho, and each track can still
deviate. rho is picked on a grid like the other hyperparameters, with a prior around
0.7: a car's bars and dampers mostly do the same job everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

LENGTHSCALES = (0.2, 0.3, 0.45, 0.65, 0.9, 1.3, 1.8, 2.5)
SIGNAL_SCALES = (0.25, 0.5, 1.0, 2.0, 4.0)  # × the plausible spread
LS_PRIOR = (np.log(0.8), 0.5)  # log-normal: mean log, sd
SV_PRIOR_SD = 0.7  # sd of log(signal sd / plausible spread)
RHOS = (0.4, 0.55, 0.7, 0.85, 0.95)
RHO_PRIOR = (0.7, 0.2)  # normal: mean, sd


def _rbf(a: np.ndarray, b: np.ndarray, ls: float) -> np.ndarray:
    d2 = ((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)
    return np.exp(-0.5 * d2 / ls ** 2)


def _tracks(ga: np.ndarray, gb: np.ndarray, rho: float) -> np.ndarray:
    return rho + (1.0 - rho) * (ga[:, None] == gb[None, :])


@dataclass
class GP:
    X: np.ndarray
    y: np.ndarray
    noise: np.ndarray
    lengthscale: float
    signal_var: float
    mean: float
    _L: np.ndarray
    _alpha: np.ndarray
    log_ml: float
    groups: np.ndarray | None = None
    rho: float = 1.0

    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, noise: np.ndarray, prior_spread: float | None = None,
            groups: np.ndarray | None = None) -> "GP":
        """``prior_spread``: how much this objective plausibly varies with the setup, in
        its own units, used when the data alone can't tell (one or two points).
        ``groups``: track per row (0 = the one predictions are for), or None."""
        X, y, noise = np.asarray(X, float), np.asarray(y, float), np.asarray(noise, float)
        multi = groups is not None and bool(np.any(np.asarray(groups) != 0))
        groups = np.asarray(groups, int) if multi else None
        mean = float(np.mean(y))
        base = max(prior_spread or 0.0, float(np.std(y)) if len(y) > 2 else 0.0, 1e-6)
        best = None
        for rho in (RHOS if multi else (1.0,)):
            rho_prior = -0.5 * ((rho - RHO_PRIOR[0]) / RHO_PRIOR[1]) ** 2 if multi else 0.0
            for ls in LENGTHSCALES:
                for s in SIGNAL_SCALES:
                    sv = (s * base) ** 2
                    try:
                        L, alpha, lml = cls._solve(X, y - mean, noise, ls, sv, groups, rho)
                    except np.linalg.LinAlgError:
                        continue
                    log_prior = (-0.5 * ((np.log(ls) - LS_PRIOR[0]) / LS_PRIOR[1]) ** 2
                                 - 0.5 * (np.log(s) / SV_PRIOR_SD) ** 2 + rho_prior)
                    score = lml + log_prior
                    if best is None or score > best[0]:
                        best = (score, ls, sv, L, alpha, lml, rho)
        _, ls, sv, L, alpha, lml, rho = best
        return cls(X, y, noise, ls, sv, mean, L, alpha, lml, groups, rho)

    @staticmethod
    def _solve(X, yc, noise, ls, sv, groups=None, rho=1.0):
        K = sv * _rbf(X, X, ls)
        if groups is not None:
            K = K * _tracks(groups, groups, rho)
        K = K + np.diag(noise + 1e-9 * sv)
        L = np.linalg.cholesky(K)
        alpha = np.linalg.solve(L.T, np.linalg.solve(L, yc))
        lml = -0.5 * yc @ alpha - np.log(np.diag(L)).sum() - 0.5 * len(yc) * np.log(2 * np.pi)
        return L, alpha, float(lml)

    def predict(self, Xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Posterior mean and variance of the underlying (noise-free) objective, on the
        track of group 0."""
        Xs = np.atleast_2d(np.asarray(Xs, float))
        Ks = self.signal_var * _rbf(Xs, self.X, self.lengthscale)
        if self.groups is not None:
            Ks = Ks * _tracks(np.zeros(len(Xs), int), self.groups, self.rho)
        mu = self.mean + Ks @ self._alpha
        v = np.linalg.solve(self._L, Ks.T)
        var = np.maximum(self.signal_var - (v * v).sum(0), 1e-12)
        return mu, var
