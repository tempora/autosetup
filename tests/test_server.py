import datetime
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
    status, health = call(base, "/api/health")
    assert status == 200 and health["ok"] and health["ui"] > 0  # the page reloads when web/ changes
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        page = r.read()
        for id_ in (b"screen-home", b"screen-analysis", b"screen-setup", b"screen-history", b"screen-driver",
                    b"screen-settings", b'id="map"', b'id="deckc"', b'id="dock"', b'id="scrub"', b'id="tiles"',
                    b'id="panel"', b'id="about"'):
            assert id_ in page, id_
        for gone in (b"rate-details", b"change-size", b"/api/feel", b"car-feel-row", b"fonts.googleapis.com"):
            assert gone not in page, gone
    for asset, ctype in (("/style.css", "text/css"), ("/app.js", "text/javascript"), ("/map.js", "text/javascript"),
                         ("/traces.js", "text/javascript"), ("/setup.js", "text/javascript"),
                         ("/cars.js", "text/javascript"), ("/state.js", "text/javascript"),
                         ("/fonts/ibm-plex-sans-latin.woff2", "font/woff2")):
        with urllib.request.urlopen(base + asset, timeout=10) as r:
            assert r.status == 200 and ctype in r.headers["Content-Type"] and len(r.read()) > 100
    # every visible text comes from the strings file
    status, strings = call(base, "/i18n/en.json")
    assert status == 200 and strings["tab.analysis"] == "Lap Analysis"
    assert call(base, "/i18n/languages.json")[1]["en"] == "English"
    assert call(base, "/i18n/..%2Fserver.py")[0] == 404


def test_import_state_stints(server):
    app, base = server
    status, data = call(base, "/api/state")
    assert status == 200 and set(data) == {"ui", "watcher", "live"}
    assert data["ui"] > 0 and data["watcher"]["imported"] == 0 and data["live"]["state"] in {"off", "menu", "live"}
    assert call(base, "/api/import", {}) == (200, {"imported": 1})
    _, data = call(base, "/api/state")
    assert data["watcher"]["imported"] == 1
    key = "Test Team 2025 #1|Test Raceway"
    _, st = call(base, "/api/stints?context=" + urllib.request.quote(key))
    assert len(st["stints"]) == 1 and st["stints"][0]["summary"]["clean_laps"] == 2
    for unused in ("/api/pareto", "/api/table", "/api/target", "/api/setup-diff"):
        assert call(base, unused)[0] == 404


def test_posts_validate_input(server):
    _, base = server
    call(base, "/api/import", {})
    # No sliders, no weights, and no unused endpoints.
    for unused in ("/api/weights", "/api/change-size", "/api/feel", "/api/context", "/api/propose"):
        assert call(base, unused, {})[0] == 404
    state = call(base, "/api/state")[1]
    assert set(state) == {"ui", "watcher", "live"}
    name = "Test Raceway_P_2026-01-01T10_00_00Z.duckdb"
    status, r = call(base, "/api/rating", {"file_name": name, "lockups": True, "note": "long brakes"})
    assert status == 200 and r["rating"]["lockups"] is True and r["rating"]["note"] == "long brakes"
    _, r = call(base, "/api/rating", {"file_name": name, "note": None})  # only the fields sent change
    assert r["rating"]["lockups"] is True
    assert call(base, "/api/feedback", {"stint": 999, "axis": "front", "value": -1})[0] == 400
    assert call(base, "/api/nope", {})[0] == 404


def test_only_the_dashboard_itself_may_post(server):
    _, base = server

    def post(origin):
        req = urllib.request.Request(base + "/api/settings", data=b"{}", method="POST",
                                     headers={} if origin is None else {"Origin": origin})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    assert post(None) == 200  # a script: no browser, no Origin
    assert post(base) == 200  # the page served from here
    assert post("https://example.com") == 403  # a page of another site, open in the same browser
    assert post("null") == 403  # a sandboxed frame or a local file


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
    import os
    import time

    import autosetup.server as server_mod

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
    assert app.advisor._lock is not app.lock
    _, pr = call(base, f"/api/proposal?stint={stint}")
    assert len(app.advisor._proposals) == 1 and app.advisor.proposal_json(stint) == pr  # cached until invalidated
    assert pr["brake_pressure_by_feel"] is False  # not a prototype: no button
    [c] = pr["changes"]  # no corner passes on the synthetic stint: only the rules speak
    assert pr["why"] is None  # there are changes
    assert c["keys"] == ["WM_CAMBER-W_FL", "WM_CAMBER-W_FR"] and "less negative camber" in c["reason"]
    assert pr["plan"]["target"]["cells"] == {} and pr["notes"] == ["No corner passes yet"]
    assert pr["target"]["file"] == f"Testway Test Car {datetime.date.today():%Y-%m-%d} 1.svm" and pr["target"]["folder"] == "Testway"
    assert pr["target"]["notes"].startswith("From stint 1 (1:00.00), As it was. Camber FL/FR -2.2 deg")

    # A stale signature is refused; the right one writes into the track's folder.
    assert call(base, "/api/write", {"stint": stint, "signature": "stale"})[0] == 409
    status, w = call(base, "/api/write", {"stint": stint, "signature": pr["signature"]})
    assert status == 200 and w["file"] == pr["target"]["file"], w
    assert (install.settings / "Testway" / w["file"]).is_file()
    assert app.advisor.written(stint)[0]["file"] == w["file"]

    # A locked setting is left alone.
    camber = ["WM_CAMBER-W_FL", "WM_CAMBER-W_FR"]
    status, lk = call(base, "/api/locks", {"stint": stint, "keys": camber, "locked": True})
    assert status == 200 and lk["locks"] == camber
    _, pr = call(base, f"/api/proposal?stint={stint}")
    assert pr["changes"] == [] and [x["key"] for x in pr["locks"]] == camber
    assert pr["why"]["text"].startswith("No corners measured") and "locked: 2" in pr["why"]["facts"]
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
        _, pr = call(base, f"/api/proposal?stint={stint}")
        assert pr["plan"]["target"]["anchor"] == "current" and pr["plan"]["measured"]["cells"]
        before = pr["plan"]["target"]["cells"]["entry_low"]
        _, t = call(base, "/api/feedback", {"stint": stint, "axis": "front", "value": -0.25})
        assert t["target"]["front"] == -0.25 and t["target"]["anchor"] == "moved"
        assert t["target"]["cells"]["entry_low"] < before and t["target"]["deadzone"]["front"] > 0
        _, t = call(base, "/api/feedback", {"stint": stint, "axis": "front", "value": 0})
        assert t["target"]["front"] == 0 and t["target"]["anchor"] == "current"
        _, t = call(base, "/api/feedback", {"stint": stint, "axis": "front", "value": -2})
        assert t["target"]["front"] == -t["target"]["span"]  # past the end: the end
        call(base, "/api/feedback", {"stint": stint, "axis": "front", "value": 0})
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
        assert call(base, f"/api/proposal?stint={stint}")[1]["plan"]["target"]["anchor"] == "current"
        _, h = call(base, f"/api/histograms?stint={stint}&kerbs=off&bin=25")
        assert set(h["wheels"]) == {"fl", "fr", "rl", "rr"} and h["knee"] > 0
        fl = h["wheels"]["fl"]
        assert len(fl["edges"]) == len(fl["counts"]) + 1 and abs(fl["stats"]["bump"] + fl["stats"]["rebound"] - 1) < 0.02
    finally:
        httpd.shutdown()
        httpd.server_close()
        store.close()


def test_lap_loads_all_stint_laps_in_one_open_without_temp_copy(server, install, monkeypatch):
    import os
    import time

    from autosetup.lmu.telemetry import Session

    rec = install.telemetry / "Test Raceway_P_2026-01-01T10_00_00Z.duckdb"
    os.utime(rec, (time.time() - 600, time.time() - 600))  # finished recording
    _, base = server
    call(base, "/api/import", {})
    stint = call(base, "/api/stints")[1]["stints"][0]["id"]
    opens = []
    real_open = Session.open

    def spy_open(path, *, copy=True):
        opens.append((path.name, copy))
        return real_open(path, copy=copy)

    monkeypatch.setattr(Session, "open", spy_open)
    status1, lap1 = call(base, f"/api/lap?stint={stint}&seq=1")
    status2, lap2 = call(base, f"/api/lap?stint={stint}&seq=2")
    assert status1 == 200 and lap1["lap"] == 2 and lap1["clean"]
    assert status2 == 200 and lap2["lap"] == 3 and lap2["clean"]
    assert opens == [(rec.name, False)]  # one open, no temp copy, both laps built
    tr = lap1["trace"]
    assert tr["step_m"] == 5.0 and len(tr["distance"]) == len(tr["time"]) == len(tr["speed"])
    assert "us_mid" in lap1["metrics"] or "lap_time" in lap1["metrics"]
    assert call(base, f"/api/lap?stint={stint}&seq=99")[0] == 400


def test_menu_screens_and_phone_access(server, install):
    app, base = server
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        page = r.read()
        for id_ in (b'id="menu-btn"', b"screen-driver", b"screen-settings", b'id="about"', b'data-go="home"'):
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
    assert s["phone_access"] is False
    assert [f["which"] for f in s["folders"]] == ["install", "telemetry", "settings", "database"]
    assert [x["car_class"] for x in s["classes"]] == [c["car_class"]]
    _, s = call(base, "/api/settings", {"units": {"temp": "°F"}, "phone_access": True})
    assert s["units"]["temp"] == "°F" and s["units"]["speed"] == "km/h" and s["phone_access"] is True
    assert call(base, "/api/open-folder", {"which": "nowhere"})[0] == 400


def test_units_convert_our_numbers():
    from autosetup.units import Units

    f = Units.from_json({"temp": "°F", "pressure": "psi", "speed": "nonsense"})
    assert f.speed == "km/h"  # unknown choices fall back
    assert f.fmt_temp(100, 0) == "212 °F" and f.fmt_temp(10, 0, delta=True) == "18 °F"  # a difference has no offset
    assert f.fmt_pressure(172) == "24.9 psi"
    assert Units().fmt_temp(4.94, delta=True) == "4.9 °C"
