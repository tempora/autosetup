import numpy as np
import pytest

from autosetup.metrics.filters import savgol, window_for


def test_window_is_odd_and_scales_with_rate():
    assert window_for(100, 0.04) == 5
    assert window_for(200, 0.04) == 9
    assert window_for(50, 0.04) == 5  # never shorter than the polynomial needs
    assert window_for(100, 0.1) % 2 == 1


@pytest.mark.parametrize("rate, tol", [(50, 0.07), (100, 0.03), (200, 0.03)])
def test_derivative_of_a_sine_at_any_rate(rate, tol):
    """Shaft velocity from deflection: a 2 Hz, 10 mm sine and small noise. At 100 Hz (the
    game's rate) and up the 40 ms window holds 3 %; at 50 Hz the shortest usable window is
    100 ms, so it rounds the peaks more."""
    rng = np.random.default_rng(0)
    t = np.arange(0, 5, 1 / rate)
    x = 0.01 * np.sin(2 * np.pi * 2 * t) + rng.normal(0, 2e-6, len(t))
    v = savgol(x, window_for(rate, 0.04), 2, deriv=1, dt=1 / rate)
    true = 0.01 * 2 * np.pi * 2 * np.cos(2 * np.pi * 2 * t)
    inner = slice(rate // 10, -rate // 10)
    assert np.max(np.abs(v[inner] - true[inner])) < tol * np.max(np.abs(true))
    assert np.max(np.abs(v - true)) < 2 * tol * np.max(np.abs(true))  # edges fitted, not padded


def test_polynomial_is_exact_including_edges():
    t = np.arange(50) * 0.01
    x = 3 + 2 * t - 4 * t ** 2
    assert np.allclose(savgol(x, 7, 2), x)
    assert np.allclose(savgol(x, 7, 2, deriv=1, dt=0.01), 2 - 8 * t)


def test_columns_and_nans():
    t = np.arange(100) * 0.01
    x = np.column_stack([t, 2 * t])
    x[40, 1] = np.nan
    v = savgol(x, 5, 2, deriv=1, dt=0.01)
    assert np.allclose(v[:, 0], 1.0)
    assert np.isnan(v[40, 1]) and np.allclose(np.delete(v[:, 1], 40), 2.0)
