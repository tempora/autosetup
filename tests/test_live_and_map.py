import numpy as np
import pytest

from autosetup.lmu.telemetry import Session
from autosetup.live import LiveFeed, parse_player
from autosetup.store import Store
from autosetup.trackmap import best_clean_lap, build_track_map, lap_trace, track_map
from tests.conftest import LAP_LEN, make_recording


def _gps_recording(path, laps=3):
    """Fixture recording plus a circular GPS track: one lap = one circle."""
    import duckdb

    rec = make_recording(path, laps=laps)
    con = duckdb.connect(str(rec))
    n = con.execute('select count(*) from "Lap Dist"').fetchone()[0]
    d = np.array([r[0] for r in con.execute('select value from "Lap Dist"').fetchall()])
    ang = d / LAP_LEN * 2 * np.pi
    radius = LAP_LEN / (2 * np.pi)
    lat = 60.0 + np.degrees(radius * np.sin(ang) / 6371000.0)
    lon = np.degrees(radius * np.cos(ang) / (6371000.0 * np.cos(np.radians(60.0))))
    for name, vals in (("GPS Latitude", lat), ("GPS Longitude", lon)):
        con.execute(f'create table "{name}" (value DOUBLE)')
        con.execute(f'insert into "{name}" select unnest(?::DOUBLE[])', [vals.tolist()])
        con.execute("insert into channelsList values (?, 50, 'deg')", [name])
    con.execute('create table "Current Sector" (ts DOUBLE, value INTEGER)')
    con.close()
    assert n == len(d)
    return rec


def test_track_map_follows_the_gps_trace(tmp_path):
    rec = _gps_recording(tmp_path / "map.duckdb")
    with Session.open(rec) as s:
        m = build_track_map(s)
    pts = np.array(m["points"])
    assert m["length"] == pytest.approx(LAP_LEN, rel=0.01)
    assert len(pts) == pytest.approx(LAP_LEN / 10, abs=2)
    r = np.hypot(pts[:, 1], pts[:, 2])
    assert r == pytest.approx(LAP_LEN / (2 * np.pi), rel=0.02)  # it's the circle
    assert np.all(np.diff(pts[:, 0]) > 0)


def test_track_map_is_cached_and_profiles_compare_to_the_best_lap(tmp_path):
    tele = tmp_path / "tele"
    tele.mkdir()
    rec = _gps_recording(tele / "a.duckdb")
    with Store(tmp_path / "db.duckdb") as store:
        rid = store.import_recording(rec)
        layout = "Test Raceway"
        first = track_map(store, layout, tele)
        assert first is not None
        rec.unlink()  # cached: no longer needs the file
        assert track_map(store, layout, tele) == first
        ref = best_clean_lap(store, "Test Team 2025 #1", layout)
        assert ref["time"] == pytest.approx(60.0) and len(ref["sectors"]) == 30
        assert "origin" in first


def test_parse_player_reads_what_it_can():
    standings = [
        {"player": False, "lapDistance": 100.0},
        {"player": True, "lapDistance": 1234.5, "timeIntoLap": 42.1, "lastLapTime": 124.2, "bestLapTime": -1.0,
         "sector": "SECTOR2", "pitting": False, "carVelocity": {"x": 30.0, "y": 0.0, "z": 40.0},
         "vehicleName": "Iron Dames #85:LM"},
    ]
    car = parse_player(standings, {"trackName": "Sebring International Raceway"})
    assert car["lap_distance"] == 1234.5 and car["time_into_lap"] == 42.1
    assert car["last_lap"] == 124.2 and car["best_lap"] is None  # -1 = no lap yet
    assert car["sector"] == 2 and not car["in_pits"]
    assert car["speed_kmh"] == pytest.approx(180.0)
    assert car["track"] == "Sebring International Raceway"
    assert car["vehicle"] == "Iron Dames #85"  # the ':LM' variant suffix is dropped, as in recordings
    assert parse_player([{"player": False}], None) is None
    assert parse_player({"weird": 1}, None) is None


def test_live_feed_states():
    calls = []

    def fetch(path):
        calls.append(path)
        if path.endswith("standings"):
            return [{"player": True, "lapDistance": 10.0}]
        return {"trackName": "T"}

    feed = LiveFeed(game_running=lambda: True, fetch=fetch)
    assert feed.poll_once()["state"] == "live"
    assert feed.snapshot()["car"]["lap_distance"] == 10.0

    def offline(path):
        raise OSError("refused")

    assert LiveFeed(game_running=lambda: True, fetch=offline).poll_once()["state"] == "menu"
    assert LiveFeed(game_running=lambda: False, fetch=fetch).poll_once()["state"] == "off"


def test_lap_trace_lines_up_with_the_map(tmp_path):
    rec = _gps_recording(tmp_path / "t.duckdb")
    with Session.open(rec) as s:
        m = build_track_map(s)
        lap = [l for l in s.laps() if l.complete][0]
        tr = lap_trace(s, lap, tuple(m["origin"]), m["length"], [1000.0, 2000.0])
    assert tr["distance"][0] == 0 and tr["step_m"] == 5.0
    assert tr["time"][-1] == pytest.approx(60.0, abs=0.2)
    assert sum(tr["sectors"]) == pytest.approx(lap.time) and len(tr["sectors"]) == 3
    # The driven line sits on the outline at the same distance.
    pts = {round(p[0]): (p[1], p[2]) for p in m["points"]}
    for i in range(0, len(tr["distance"]), 40):
        d = round(tr["distance"][i])
        if d in pts:
            assert abs(tr["x"][i] - pts[d][0]) < 1.0 and abs(tr["y"][i] - pts[d][1]) < 1.0


def test_open_session_maps_to_a_context(tmp_path, install):
    from autosetup.server import App

    with Store(tmp_path / "db.duckdb") as store:
        store.import_all(install.telemetry)
        app = App(store, install)
        app.live.snapshot = lambda: {"state": "live", "car": {"vehicle": "Test Team 2025 #1", "track": "Test Raceway"}}
        assert app.open_session()["context"] == "Test Team 2025 #1|Test Raceway"
        app.live.snapshot = lambda: {"state": "live", "car": {"vehicle": "Someone Else #9", "track": "Test Raceway"}}
        assert app.open_session()["context"] is None
        app.live.snapshot = lambda: {"state": "off"}
        assert app.open_session() == {"state": "off", "vehicle": None, "track": None, "session": None, "context": None}


def test_lap_distance_does_not_blend_across_the_line():
    """The 4652 m → 0 m step at the line must not be interpolated into ~2300 m (that
    flattened the first half of a trace and gave a 0.005 s sector)."""
    from autosetup.lmu.telemetry import Lap
    from autosetup.trackmap import _lap_distance

    length = 4653.4
    td = np.array([99.98, 100.0, 100.02, 100.04])
    dist = np.array([4651.0, 4652.3, 0.0, 1.5])
    lap = Lap(2, 100.01, 190.0, True, False)
    d = _lap_distance(np.array([100.01, 100.015, 100.03, 100.04]), td, dist, lap, length)
    assert d.max() < 2.0 and (np.diff(d) >= 0).all()
