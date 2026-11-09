"""History sessions and the Welcome fact (UI_LAYOUT_CONTRACT.md §2, §5)."""

import random

from autosetup import screens
from autosetup.store import Store
from tests.conftest import make_recording
from tests.test_server import call, server  # noqa: F401  (fixture)

T0 = 1_700_000_000


def _import(store, install, name, t, track="Test Raceway", **values):
    return store.import_recording(make_recording(install.telemetry / name, laps=4, values=values, track=track,
                                                 mtime=T0 + t))


def _recorded(store, rid, iso):
    store.con.execute("update recordings set recorded_at = ? where id = ?", [iso, rid])


def test_sessions_split_on_long_gaps_and_by_car_track(tmp_path, install):
    with Store(tmp_path / "db.duckdb") as store:
        a = _import(store, install, "a.duckdb", 0)
        b = _import(store, install, "b.duckdb", 10, VM_REAR_ANTISWAY=4)
        c = _import(store, install, "c.duckdb", 20)
        d = _import(store, install, "d.duckdb", 30, track="Other Ring")
        _recorded(store, a, "2026-01-01T10_00_00Z")
        _recorded(store, b, "2026-01-01T10_30_00Z")  # same sitting
        _recorded(store, c, "2026-01-02T09_00_00Z")  # next day: a new session
        _recorded(store, d, "2026-01-02T10_00_00Z")
        ss = screens.sessions(store)
        assert [s["stint_ids"] for s in ss] == [[d], [c], [a, b]]  # newest first
        assert ss[2]["stints"] == 2 and ss[2]["laps"] == 8 and ss[2]["best"] is not None
        assert screens.latest_session(store, ss[1]["context"])["id"] == c


def test_fun_fact_is_about_your_driving(tmp_path, install):
    with Store(tmp_path / "db.duckdb") as store:
        assert screens.fun_fact(store) is None
        _import(store, install, "a.duckdb", 0)
        fact = screens.fun_fact(store, random.Random(1))
        assert fact and ("lap" in fact)


def test_screen_endpoints(server):  # noqa: F811
    app, base = server
    call(base, "/api/import", {})
    status, data = call(base, "/api/sessions")
    assert status == 200 and len(data["sessions"]) == 1
    sid = data["sessions"][0]["id"]
    assert call(base, "/api/welcome")[1]["fact"]
    status, st = call(base, f"/api/stints?session={sid}")
    assert status == 200 and st["session"] == sid and len(st["stints"]) == 1
    assert call(base, f"/api/table?session={sid}&by=lap")[0] == 404
