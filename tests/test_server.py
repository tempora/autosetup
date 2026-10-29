import json
import threading
import urllib.error
import urllib.request

import pytest

from autosetup import units
from autosetup.server import SETTLE_S, App, make_server
from autosetup.store import Store
from tests.conftest import make_recording


@pytest.fixture
def server(tmp_path, install):
    store = Store(tmp_path / "db.duckdb")
    app = App(store, install)
    httpd = make_server(app, "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield app, base
    httpd.shutdown()
    httpd.server_close()
    store.close()
    units.set_current(units.Units())  # display units are process-wide


def call(base, path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health_and_page(server):
    _, base = server
    assert call(base, "/api/health") == (200, {"ok": True})
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        page = r.read()
        for id_ in (b"screen-welcome", b"screen-analysis", b"screen-setup", b"screen-history", b'id="map"',
                    b'id="slider"', b'id="traces"', b'id="modal"', b'id="vis"', b'id="advanced"', b'id="feel-rear"',
                    b'id="feel-front"', b'id="this-is-it"', b'id="gap"'):
            assert id_ in page, id_
        for gone in (b"rate-details", b"change-size", b"/api/feel", b"car-feel-row"):
            assert gone not in page, gone


def test_import_state_stints_pareto(server):
    app, base = server
    status, data = call(base, "/api/state")
    assert status == 200 and data["contexts"] == [] and data["context"] is None
    assert call(base, "/api/import", {}) == (200, {"imported": 1})
    _, data = call(base, "/api/state")
    key = data["context"]
    assert key == "Test Team 2025 #1|Test Raceway"
    assert [o["key"] for o in data["objectives"]] == ["median", "wear_per_lap", "consistency"]
    _, st = call(base, "/api/stints?context=" + urllib.request.quote(key))
    assert len(st["stints"]) == 1 and st["stints"][0]["summary"]["clean_laps"] == 2
    _, p = call(base, "/api/pareto")
    assert p["context"] == key and len(p["points"]) == 1


def test_posts_validate_input(server):
    _, base = server
    call(base, "/api/import", {})
    # No sliders, no weights: lap time is the objective, your balance the constraint.
    assert call(base, "/api/weights", {"pace": 1, "tyres": 0.2, "drivability": 0.5})[0] == 404
    assert call(base, "/api/change-size", {"size": "small"})[0] == 404
    assert call(base, "/api/feel", {"car_class": "GT3", "front": -1})[0] == 404
    state = call(base, "/api/state")[1]
    assert "weights" not in state and "change_size" not in state
    assert call(base, "/api/context", {"context": "nope"})[0] == 400
    name = "Test Raceway_P_2026-01-01T10_00_00Z.duckdb"
    status, r = call(base, "/api/rating", {"file_name": name, "lockups": True, "note": "long brakes"})
    assert status == 200 and r["rating"]["lockups"] is True and r["rating"]["note"] == "long brakes"
    _, r = call(base, "/api/rating", {"file_name": name, "note": None})  # only the fields sent change
    assert r["rating"]["lockups"] is True
    assert call(base, "/api/feedback", {"stint": 999, "axis": "front", "value": -1})[0] == 400
    assert call(base, "/api/nope", {})[0] == 404


def test_watcher_waits_for_files_to_settle(tmp_path, install):
    with Store(tmp_path / "db.duckdb") as store:
        app = App(store, install)
        assert app.poll(now=1000.0) == 0  # first sighting
        assert app.poll(now=1000.0 + SETTLE_S / 2) == 0  # not settled yet
        assert app.poll(now=1000.0 + SETTLE_S) == 1
        make_recording(install.telemetry / "second.duckdb")
        assert app.poll(now=2000.0) == 0
        with open(install.telemetry / "second.duckdb", "ab") as f:
            f.write(b"\0" * 1024)  # still growing: the clock restarts
        assert app.poll(now=2000.0 + SETTLE_S) == 0
        assert app.poll(now=2000.0 + 2 * SETTLE_S) == 1
        assert app.status["imported"] == 2


def test_a_backlog_is_imported_newest_first_in_rounds(tmp_path, install, monkeypatch):
    import os

    import autosetup.server as server_mod

    monkeypatch.setattr(server_mod, "IMPORT_BATCH", 2)
    oldest = install.telemetry / "Test Raceway_P_2026-01-01T10_00_00Z.duckdb"
    os.utime(oldest, (900.0, 900.0))
    for i in range(5):
        make_recording(install.telemetry / f"old{i}.duckdb", mtime=1000.0 + i)
    with Store(tmp_path / "db.duckdb") as store:
        app = App(store, install)
        names = lambda: {r[0] for r in store.con.execute("select file_name from recordings").fetchall()}
        assert app.poll(now=5000.0) == 0  # first sighting
        assert app.poll(now=5000.0 + SETTLE_S) == 2
        assert names() == {"old4.duckdb", "old3.duckdb"}
        make_recording(install.telemetry / "fresh.duckdb", mtime=2000.0)  # an outing finished meanwhile
        assert app.poll(now=6000.0) == 2  # still settling: the backlog goes on
        assert names() == {"old4.duckdb", "old3.duckdb", "old2.duckdb", "old1.duckdb"}
        assert app.poll(now=6000.0 + SETTLE_S) == 2  # settled: ahead of the rest of the backlog
        assert "fresh.duckdb" in names() and oldest.name not in names()
        assert app.poll(now=7000.0) == 1
        assert app.poll(now=8000.0) == 0


def test_a_broken_recording_is_tried_once_not_every_poll(tmp_path, install, monkeypatch):
    import autosetup.server as server_mod

    import os
    import time

    broken = install.telemetry / "broken.duckdb"

    def write_old(data: bytes) -> None:  # a file written minutes ago (a fresh one may be mid-write: busy)
        broken.write_bytes(data)
        os.utime(broken, (time.time() - 600, time.time() - 600))

    write_old(b"not a duckdb file" * 100)
    calls = []
    real = server_mod.read_recording

    def counting(path):
        calls.append(path.name)
        return real(path)

    monkeypatch.setattr(server_mod, "read_recording", counting)
    with Store(tmp_path / "db.duckdb") as store:
        app = App(store, install)
        app.import_now()
        assert calls.count("broken.duckdb") == 1 and "broken.duckdb" in app.status["last_error"]
        app.import_now()
        app.import_now()
        assert calls.count("broken.duckdb") == 1  # unchanged since: skipped
        write_old(b"still not a duckdb file" * 100)
        app.import_now()
        assert calls.count("broken.duckdb") == 2  # changed: tried again


def test_a_setup_saved_after_its_stint_is_matched_without_an_import(tmp_path, install):
    saved = install.settings / "Testway" / "base.svm"
    text = saved.read_bytes()
    saved.unlink()
    with Store(tmp_path / "db.duckdb") as store:
        app = App(store, install)
        assert app.import_now() == 1
        assert store.folder_for("Test Raceway") is None
        app.poll()  # takes the stamp of the saved setups
        app.status["last_import"] = None
        saved.write_bytes(text)  # saved in the garage
        app.poll()
        assert store.folder_for("Test Raceway") == "Testway"
        assert app.status["last_import"] is not None  # the page reloads stints and proposal


def test_proposal_write_and_locks(server, install):
    app, base = server
    rec = install.telemetry / "Test Raceway_P_2026-01-01T10_00_00Z.duckdb"
    rec.unlink()
    make_recording(rec, tyres=True)
    call(base, "/api/import", {})
    stint = call(base, "/api/stints")[1]["stints"][0]["id"]
    _, pr = call(base, f"/api/proposal?stint={stint}")
    assert pr["brake_pressure_by_feel"] is False  # not a prototype: no button
    [c] = pr["changes"]  # no corner passes on the synthetic stint: only the rules speak
    assert c["keys"] == ["WM_CAMBER-W_FL", "WM_CAMBER-W_FR"] and "less negative camber" in c["reason"]
    assert pr["plan"]["target"]["cells"] == {} and pr["notes"] == ["No corner passes yet"]
    assert pr["target"] == {"file": "autosetup_TestTeam1_Testway_1.svm", "folder": "Testway"}

    # A stale signature is refused; the right one writes into the track's folder.
    assert call(base, "/api/write", {"stint": stint, "signature": "stale"})[0] == 409
    status, w = call(base, "/api/write", {"stint": stint, "signature": pr["signature"]})
    assert status == 200 and w["file"] == "autosetup_TestTeam1_Testway_1.svm"
    assert (install.settings / "Testway" / w["file"]).is_file()
    assert app.advisor.written(stint)[0]["file"] == w["file"]

    # A locked setting is left alone.
    camber = ["WM_CAMBER-W_FL", "WM_CAMBER-W_FR"]
    status, lk = call(base, "/api/locks", {"stint": stint, "keys": camber, "locked": True})
    assert status == 200 and lk["locks"] == camber
    _, pr = call(base, f"/api/proposal?stint={stint}")
    assert pr["changes"] == [] and [x["key"] for x in pr["locks"]] == camber
    call(base, "/api/locks", {"stint": stint, "keys": camber, "locked": False})
    assert call(base, f"/api/proposal?stint={stint}")[1]["locks"] == []
    assert call(base, "/api/locks", {"stint": stint, "keys": "VM_X"})[0] == 400
    assert call(base, "/api/proposal?stint=999")[0] == 400


def test_buttons_lock_histograms_and_driver_on_a_recorded_stint(tmp_path, recorded):
    """A real Interlagos stint: the target moves with the buttons, "This is it" locks a
    fingerprint the Driver screen shows, histograms come per wheel."""
    import shutil

    from autosetup.lmu.paths import LmuPaths

    root = tmp_path / "Le Mans Ultimate"
    paths = LmuPaths(root)
    paths.telemetry.mkdir(parents=True)
    paths.settings.mkdir(parents=True)
    src = recorded("interlagos_arb_r5")
    shutil.copyfile(src, paths.telemetry / src.name)
    store = Store(tmp_path / "db.duckdb")
    app = App(store, paths)
    httpd = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        call(base, "/api/import", {})
        stint = call(base, "/api/stints")[1]["stints"][0]["id"]
        _, t = call(base, f"/api/target?stint={stint}")
        assert t["target"]["anchor"] == "current" and t["measured"]["cells"]
        before = t["target"]["cells"]["entry_low"]
        _, t = call(base, "/api/feedback", {"stint": stint, "axis": "front", "value": -1})
        assert t["target"]["front"] == -1 and t["target"]["anchor"] == "toggled"
        assert t["target"]["cells"]["entry_low"] < before
        _, t = call(base, "/api/feedback", {"stint": stint, "axis": "front", "value": 0})
        assert t["target"]["front"] == 0 and t["target"]["anchor"] == "current"
        assert call(base, "/api/feedback", {"stint": stint, "axis": "front", "value": -2})[0] == 400
        assert call(base, "/api/feedback", {"stint": stint, "axis": "faster", "value": 1})[0] == 400
        _, pr = call(base, f"/api/proposal?stint={stint}")
        assert set(pr["plan"]["target"]["cells"]) and pr["plan"]["lap"] is not None
        plat = pr["plan"]["platform"]
        assert set(plat) == {"current", "proposed"} and plat["current"]["rake"] is not None
        _, lk = call(base, "/api/lock", {"stint": stint})
        assert lk["fingerprint"]["stint"] == stint and lk["target"]["anchor"] == "fingerprint"
        _, d = call(base, "/api/driver")
        [cls] = d["classes"]
        [car] = cls["cars"]
        assert car["line"] and abs(car["rear"]) < 10 and abs(car["front"]) < 10
        _, s = call(base, "/api/settings")
        assert [f["car_key"] for f in s["fingerprints"]] == [car["car_key"]]
        assert call(base, "/api/forget", {"car_key": car["car_key"]}) == (200, {"forgotten": 1})
        assert call(base, "/api/settings")[1]["fingerprints"] == []
        assert call(base, "/api/driver")[1]["classes"][0]["cars"] == []
        assert call(base, f"/api/target?stint={stint}")[1]["target"]["anchor"] == "current"
        _, h = call(base, f"/api/histograms?stint={stint}&kerbs=off&bin=25")
        assert set(h["wheels"]) == {"fl", "fr", "rl", "rr"} and h["knee"] > 0
        fl = h["wheels"]["fl"]
        assert len(fl["edges"]) == len(fl["counts"]) + 1 and abs(fl["stats"]["bump"] + fl["stats"]["rebound"] - 1) < 0.02
    finally:
        httpd.shutdown()
        httpd.server_close()
        store.close()


def test_lap_and_setup_diff_endpoints(server, install):
    _, base = server
    call(base, "/api/import", {})
    stint = call(base, "/api/stints")[1]["stints"][0]["id"]
    status, lap = call(base, f"/api/lap?stint={stint}&seq=1")
    assert status == 200 and lap["lap"] == 2 and lap["clean"]
    tr = lap["trace"]
    assert tr["step_m"] == 5.0 and len(tr["distance"]) == len(tr["time"]) == len(tr["speed"])
    assert "us_mid" in lap["metrics"] or "lap_time" in lap["metrics"]
    assert call(base, f"/api/lap?stint={stint}&seq=99")[0] == 400
    assert call(base, f"/api/setup-diff?a={stint}&b={stint}") == (200, {"changes": []})


def test_menu_screens(server, install):
    app, base = server
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        page = r.read()
        for id_ in (b'id="menu-btn"', b"screen-driver", b"screen-settings", b'id="about"', b"page-close"):
            assert id_ in page, id_
        for gone in (b'id="keys"', b'id="sheet"', b"theme-toggle"):  # no shortcuts, Analysis sheet or nav toggle
            assert gone not in page, gone
    call(base, "/api/import", {})
    _, d = call(base, "/api/driver")
    [c] = d["classes"]
    assert c["laps"] > 0 and c["cars"] == []  # no fingerprint locked yet
    _, a = call(base, "/api/about")
    assert a["recordings"] == 1 and a["laps"] > 0 and a["database"].endswith("db.duckdb")

    _, s = call(base, "/api/settings")
    assert s["units"] == {"speed": "km/h", "temp": "°C", "pressure": "kPa"}
    assert [f["which"] for f in s["folders"]] == ["install", "telemetry", "settings", "database"]
    assert [x["car_class"] for x in s["classes"]] == [c["car_class"]]
    _, s = call(base, "/api/settings", {"units": {"temp": "°F"}})
    assert s["units"]["temp"] == "°F" and s["units"]["speed"] == "km/h"
    assert call(base, "/api/open-folder", {"which": "nowhere"})[0] == 400


def test_units_convert_our_numbers():
    from autosetup.units import Units

    f = Units.from_json({"temp": "°F", "pressure": "psi", "speed": "nonsense"})
    assert f.speed == "km/h"  # unknown choices fall back
    assert f.fmt_temp(100, 0) == "212 °F" and f.fmt_temp(10, 0, delta=True) == "18 °F"  # a difference has no offset
    assert f.fmt_pressure(172) == "24.9 psi"
    assert Units().fmt_temp(4.94, delta=True) == "4.9 °C"
