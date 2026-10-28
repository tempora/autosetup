"""Savitzky–Golay smoothing and derivatives in numpy (no scipy: the desktop bundle stays small).

A window is given in seconds and turned into samples at the channel's own rate, so the
same filter means the same thing on a 50 Hz and a 100 Hz channel. Edges are fitted with
the polynomial of the first / last full window instead of padding.
"""

from __future__ import annotations

import math

import numpy as np


def window_for(rate_hz: float, seconds: float, poly: int = 2) -> int:
    """Odd window length (samples) closest to ``seconds`` at ``rate_hz``, long enough for
    the polynomial."""
    n = int(round(seconds * rate_hz))
    n = max(n, poly + 2)
    return n if n % 2 else n + 1


def _design(window: int, poly: int) -> tuple[np.ndarray, np.ndarray]:
    half = window // 2
    x = np.arange(-half, half + 1, dtype=float)
    A = np.vander(x, poly + 1, increasing=True)  # columns 1, x, x², ...
    return x, np.linalg.pinv(A)  # (poly+1, window): polynomial coefficients from samples


def savgol(y: np.ndarray, window: int, poly: int = 2, deriv: int = 0, dt: float = 1.0) -> np.ndarray:
    """Savitzky–Golay filter of ``y`` along axis 0; ``deriv`` > 0 gives that derivative per
    unit time (``dt`` = sample spacing). NaNs are filled linearly first and put back."""
    y = np.asarray(y, dtype=float)
    if y.ndim > 1:
        return np.column_stack([savgol(y[:, i], window, poly, deriv, dt) for i in range(y.shape[1])])
    n = len(y)
    if deriv > poly:
        return np.zeros(n)
    if n < window:
        if n < poly + 1:
            return np.zeros(n) if deriv else y.copy()
        window = n if n % 2 else n - 1
        if window < poly + 1:
            return np.zeros(n) if deriv else y.copy()
    bad = ~np.isfinite(y)
    if bad.all():
        return np.full(n, np.nan)
    if bad.any():
        idx = np.arange(n)
        y = y.copy()
        y[bad] = np.interp(idx[bad], idx[~bad], y[~bad])
    x, pinv = _design(window, poly)
    scale = math.factorial(deriv) / dt ** deriv
    kernel = pinv[deriv] * scale
    # Correlation with the kernel: out[i] = sum_j kernel[j] * y[i + j - half].
    out = np.convolve(y, kernel[::-1], mode="same")
    half = window // 2
    for edge in (slice(0, window), slice(n - window, n)):
        coef = pinv @ y[edge]  # polynomial over the edge window, in samples from its centre
        pos = (np.arange(window) - half)[: half] if edge.start == 0 else (np.arange(window) - half)[-half:]
        where = np.arange(half) if edge.start == 0 else np.arange(n - half, n)
        powers = np.arange(deriv, poly + 1)
        falling = np.array([math.factorial(p) / math.factorial(p - deriv) for p in powers])
        out[where] = (coef[powers] * falling * pos[:, None] ** (powers - deriv)).sum(1) / dt ** deriv
    out[bad] = np.nan
    return out
