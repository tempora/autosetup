import numpy as np
import pytest

from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.telemetry import Session
from autosetup.metrics import compute_recording
from autosetup.metrics.compute import GRID_HZ, _minisectors, _reversals, _runs
from autosetup.metrics.signals import Signals, smooth
from tests.conftest import LAP, setup_json


def test_runs_counts_long_enough_stretches():
    m = np.array([0, 1, 1, 0, 1, 1, 1, 1, 0, 1], dtype=bool)
    assert _runs(m) == 3
    assert _runs(m, min_len=2) == 2
    assert _runs(m, min_len=4) == 1
    assert _runs(np.zeros(5, dtype=bool)) == 0


def test_reversals_count_direction_changes_beyond_the_gap():
    t = np.arange(0, 10, 1 / GRID_HZ)
    speed = np.full_like(t, 30.0)
    sweep = 20 * np.sin(2 * np.pi * 0.5 * t)  # 0.5 Hz, ±20°: one reversal per second
    assert _reversals(sweep, speed) == 10  # extremes every second, 10 s
    assert _reversals(0.5 * np.sin(2 * np.pi * 2 * t), speed) == 0  # below the 2° gap
    assert _reversals(sweep, np.zeros_like(t)) == 0  # stationary: ignored


def test_minisectors_sum_to_lap_time_even_if_distance_wraps_late():
    n = int(LAP * GRID_HZ)
    t = 100 + np.arange(n) / GRID_HZ
    dist = np.linspace(0, 2950, n)
    dist[:2] = 3000.0  # the wrap trails the lap event by two samples
    sig = Signals(**{f: np.zeros(n) for f in Signals.__dataclass_fields__})
    sig.t, sig.dist = t, dist
    sectors = _minisectors(sig, 3000.0)
    assert len(sectors) == 30
    assert sectors.sum() == pytest.approx(t[-1] - t[0], abs=0.05)
    assert (sectors > 0).all()


def test_smooth_preserves_constant_and_edges():
    x = np.full(20, 3.0)
    np.testing.assert_allclose(smooth(x, 5), x)


@pytest.mark.parametrize("hz", [50, 10])
def test_minisectors_are_even_at_any_lap_dist_rate(tmp_path, hz):
    """The fixture drives at constant speed, so every 100 m minisector takes the same
    time. A blended wrap at 10 Hz used to zero the first sectors and inflate one."""
    from tests.conftest import make_recording

    with Session.open(make_recording(tmp_path / f"r{hz}.duckdb", lap_dist_hz=hz)) as s:
        full = compute_recording(s, setup=CarSetup.from_json(setup_json()))[1]
    assert full.sectors.min() == pytest.approx(LAP / 30, abs=0.15)
    assert full.sectors.max() == pytest.approx(LAP / 30, abs=0.15)


def test_compute_survives_recordings_missing_channels(recording):
    """The synthetic recording has only a handful of channels; nothing may crash and the
    lap-level basics must still be there."""
    with Session.open(recording) as s:
        results = compute_recording(s, setup=CarSetup.from_json(setup_json()))
    assert [r.seq for r in results] == [0, 1, 2, 3]
    full = results[1]
    assert full.metrics["lap_time"] == pytest.approx(LAP)
    assert full.metrics["complete"] == 1.0
    assert full.metrics["wear_rr"] > full.metrics["wear_fl"] > 0
    assert len(full.sectors) == 30
    assert full.sectors.sum() == pytest.approx(LAP, abs=0.1)
    assert len(results[0].sectors) == 0  # partial laps get no minisectors


def test_steering_in_percent_is_normalised(tmp_path):
    """October 2026 LMU files log Steering Pos in % (-100..100) instead of -1..1."""
    import duckdb
    from tests.conftest import make_recording
    from autosetup.metrics.signals import load_signals

    rec = make_recording(tmp_path / "pct.duckdb")
    con = duckdb.connect(str(rec))
    n = con.execute('select count(*) from "GPS Time"').fetchone()[0]
    con.execute('create table "Steering Pos" (value FLOAT)')
    con.execute('insert into "Steering Pos" select 25.0 from range(?)', [n])
    con.execute("insert into channelsList values ('Steering Pos', 100, '%')")
    con.close()
    with Session.open(rec) as s:
        assert s.units["Steering Pos"] == "%"
        sig = load_signals(s)
    assert np.nanmax(sig.steer) == pytest.approx(0.25)
