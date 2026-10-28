import numpy as np

from autosetup.metrics.kerbs import events, grid_mask, kerb_mask
from autosetup.metrics.suspension import EDGES, Suspension, histogram, lap_suspension, load_cv, pitch_heave, shape
from tests.test_balance_metric import make_signals

RATE = 100.0


def _susp(n=2000, kerb=None, has_surface=True, load=None, v=None, rh=None, speed=40.0):
    t = np.arange(n) / RATE
    x = np.full((n, 4), 0.05)
    return Suspension(t=t, rate=RATE, x=x, v=v if v is not None else np.zeros((n, 4)),
                      rh=rh if rh is not None else np.full((n, 4), 0.05), load=load if load is not None else x.copy(),
                      load_source="spring", kerb=kerb if kerb is not None else np.zeros((n, 4), bool),
                      has_surface=has_surface, speed=np.full(n, speed))


def test_histogram_grid_and_shape():
    rng = np.random.default_rng(0)
    v = rng.normal(0, 40, 20000)
    h = histogram(v)
    assert len(h) == len(EDGES) - 1 and h.sum() == len(v)
    s = shape(v, knee_mm_s=40)
    assert abs(s["bump_frac"] - 0.5) < 0.02
    assert abs(s["ls_frac"] - 0.683) < 0.02  # |v| < 1 sd
    assert abs(s["skew"]) < 0.1
    assert shape(np.abs(v), 40)["skew"] > 0.5
    h = histogram(np.array([5000.0, -5000.0]))  # beyond the range: end bins
    assert h[0] == 1 and h[-1] == 1


def test_load_variation_proxy():
    x = 0.05 + 0.01 * np.sin(np.linspace(0, 20, 500))
    assert abs(load_cv(x, "spring") - 0.01 / np.sqrt(2) / 0.05) < 0.01
    assert load_cv(np.full(500, 0.001), "spring") is None  # wheel too light to judge
    assert load_cv(np.full(500, 3000.0), "tyre_load") == 0.0


def test_lap_suspension_splits_kerb_samples_and_phases():
    n = 3000
    kerb = np.zeros((n, 4), bool)
    kerb[1000:1050, 0] = True
    rng = np.random.default_rng(1)
    s = _susp(n, kerb=kerb, v=rng.normal(0, 0.03, (n, 4)), load=0.05 + rng.normal(0, 0.005, (n, 4)))
    phase = np.where(np.arange(n) < 1500, "entry", "exit")
    rows = lap_suspension(s, 0, 0.0, n / RATE, phase, knee_mm_s=50)
    by = {(r.wheel, r.kerb): r for r in rows}
    assert ("fl", True) in by and ("fr", True) not in by
    assert by[("fl", True)].n == 50
    off = by[("fl", False)]
    assert off.n == n - 50 and "load_cv" not in by[("fl", True)].stats
    assert {"load_cv", "load_cv_entry", "load_cv_exit", "bump_frac", "ls_frac", "skew"} <= set(off.stats)


def test_pitch_heave():
    rh = np.array([[0.04, 0.04, 0.06, 0.06]])
    pitch, heave = pitch_heave(rh, 2.5)
    assert pitch[0] > 0 and abs(heave[0] - 50.0) < 1e-9


def test_kerb_event_from_surface_codes():
    """One kerb strike on the front left: a load spike, then the wheel in the air for 0.1 s,
    the nose pitching, and the car wobbling after it."""
    n = 2000
    kerb = np.zeros((n, 4), bool)
    kerb[1000:1020, 0] = True
    load = np.full((n, 4), 0.05)
    load[1005:1010, 0] = 0.12
    load[1020:1030, 0] = 0.0  # airborne
    rh = np.full((n, 4), 0.05)
    rh[1010:1030, :2] = 0.06
    s = _susp(n, kerb=kerb, load=load, rh=rh)
    t50 = np.arange(n // 2) / 50.0
    sig = make_signals(len(t50), t=t50, dist=t50 * 40.0)
    err = np.zeros(len(t50))
    err[(t50 > 10.2) & (t50 < 10.6)] = 0.2
    evs = events(s, sig, err, wheelbase=2.6)
    assert len(evs) == 1
    e = evs[0]
    assert e.source == "surface" and e.wheels == "fl"
    assert abs(e.t0 - 10.0) < 0.02 and abs(e.dist - 400.0) < 2
    assert abs(e.peak_load - 0.12 / 0.05) < 1e-6
    assert abs(e.airtime_s - 0.1) < 0.011
    assert e.pitch_deg > 0.1 and e.heave_mm > 1.0
    assert e.yaw_dist > 0.1


def test_kerb_strikes_close_together_are_one_event():
    n = 2000
    kerb = np.zeros((n, 4), bool)
    kerb[1000:1010, 0] = True
    kerb[1015:1025, 2] = True  # rear wheel 0.15 s later: the same kerb
    kerb[1500:1510, 1] = True
    s = _susp(n, kerb=kerb)
    sig = make_signals(1000, t=np.arange(1000) / 50.0)
    evs = events(s, sig, np.zeros(1000), 2.6)
    assert [e.wheels for e in evs] == ["fl,rl", "fr"]


def test_spike_fallback_only_at_the_track_edge():
    """Without surface types, a shaft-velocity spike counts only where the car is at the edge."""
    n = 2000
    v = np.zeros((n, 4))
    v[500:505, 0] = 0.5  # 500 mm/s in the middle of the track
    v[1500:1505, 1] = 0.5  # and at the edge
    s = _susp(n, v=v, has_surface=False)
    t50 = np.arange(1000) / 50.0
    off = np.where((t50 > 14.5) & (t50 < 15.5), -0.3, -5.0)
    sig = make_signals(1000, t=t50, off_line=off)
    mask, source = kerb_mask(s, sig)
    assert source == "spike"
    evs = events(s, sig, np.zeros(1000), 2.6)
    assert len(evs) == 1 and evs[0].wheels == "fr" and abs(evs[0].t0 - 14.9) < 0.05
    assert grid_mask(s, mask, t50)[(t50 > 15.0) & (t50 < 15.04)].all()
