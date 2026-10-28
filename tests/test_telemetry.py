import numpy as np
import pytest

from autosetup.lmu.telemetry import Session
from tests.conftest import LAP, T0, make_recording


def test_metadata_and_setup(recording):
    with Session.open(recording) as s:
        assert s.track == "Test Raceway"
        assert s.car_name == "Test Team 2025 #1"  # ':LM' suffix dropped
        assert s.car_class == "GT3"
        assert "CarSetup" not in s.metadata
        assert s.setup_json["VM_REAR_ANTISWAY"]["value"] == 3


def test_clock_and_duration(recording):
    with Session.open(recording) as s:
        assert s.t_start == pytest.approx(T0)
        assert s.duration == pytest.approx(200.0)


@pytest.mark.parametrize("hz", [50, 10])
def test_fixed_rate_channels_align_with_lap_events(tmp_path, hz):
    """The Lap Dist wrap must land on the Lap event, whatever rate the file uses."""
    with Session.open(make_recording(tmp_path / f"r{hz}.duckdb", lap_dist_hz=hz)) as s:
        t, dist = s.channel("Lap Dist")
        wraps = t[1:][np.diff(dist) < -1000]
        crossings, _ = s.events("Lap")
        np.testing.assert_allclose(wraps, crossings[1:], atol=1.0 / hz + 1e-6)


def test_four_wheel_channel_shape(recording):
    with Session.open(recording) as s:
        t, wear = s.channel("Tyres Wear")
        assert wear.shape == (len(t), 4)
        assert wear[-1, 3] < wear[-1, 0]  # RR wears fastest in the fixture


def test_laps(recording):
    with Session.open(recording) as s:
        laps = s.laps()
    assert [lap.number for lap in laps] == [1, 2, 3, 4]
    assert [lap.complete for lap in laps] == [False, True, True, False]
    assert laps[1].time == pytest.approx(LAP)
    assert laps[2].time == pytest.approx(LAP)
    assert [lap.in_pits for lap in laps] == [False, False, False, True]


def test_state_at(recording):
    with Session.open(recording) as s:
        assert s.state_at("Lap", T0 + 31.0) == 2
        np.testing.assert_array_equal(s.state_at("In Pits", np.array([T0, T0 + 160.0])), [0, 1])


def test_unknown_channel_raises(recording):
    with Session.open(recording) as s:
        with pytest.raises(KeyError):
            s.channel("Lap")  # an event table, not fixed-rate
        with pytest.raises(KeyError):
            s.events("Lap Dist")


def test_reads_a_copy_and_cleans_up(recording):
    s = Session.open(recording)
    tmp = s._tmp
    assert tmp is not None and tmp.exists()
    recording.unlink()  # the original isn't held open
    s.close()
    assert not tmp.exists()
