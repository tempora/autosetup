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
                    b'id="slider"', b'id="traces"', b'id="modal"', b"rate-details"):
            assert id_ in page, id_


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
    # No sliders: the trade-off is learned, and with no evidence it's the prior.
    assert call(base, "/api/weights", {"pace": 1, "tyres": 0.2, "drivability": 0.5})[0] == 404
    state = call(base, "/api/state")[1]
    assert state["weights"]["pace"] == 1.0 and state["preference"]["comparisons"] == 0
    assert state["weights"]["tyres"] == pytest.approx(0.5, abs=0.05)
    assert call(base, "/api/context", {"context": "nope"})[0] == 400
    name = "Test Raceway_P_2026-01-01T10_00_00Z.duckdb"
    status, r = call(base, "/api/rating", {"file_name": name, "rating": 5, "entry": -1})
    assert status == 200 and r["rating"]["rating"] == 5 and r["rating"]["entry"] == -1
    assert call(base, "/api/rating", {"file_name": name, "entry": 3})[0] == 400
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


def test_proposal_write_and_locks(server, install):
    app, base = server
    call(base, "/api/import", {})
    call(base, "/api/change-size", {"size": "small"})  # one click per move keeps this exact
    stint = call(base, "/api/stints")[1]["stints"][0]["id"]
    _, pr = call(base, f"/api/proposal?stint={stint}")
    assert pr["changes"] == []  # the synthetic stint has no tyre/brake data and isn't rated
    name = "Test Raceway_P_2026-01-01T10_00_00Z.duckdb"
    # Each click sends only its own field: quick clicks on different buttons don't undo each other.
    _, r = call(base, "/api/rating", {"file_name": name, "lockups": True})
    assert r["rating"]["lockups"] is True
    _, r = call(base, "/api/rating", {"file_name": name, "entry": -1})
    _, r = call(base, "/api/rating", {"file_name": name, "mid": 1})
    assert (r["rating"]["lockups"], r["rating"]["entry"], r["rating"]["mid"]) == (True, -1, 1)
    _, r = call(base, "/api/rating", {"file_name": name, "entry": None, "lockups": False})  # null clears one
    assert (r["rating"]["lockups"], r["rating"]["entry"], r["rating"]["mid"]) == (False, None, 1)
    _, pr = call(base, "/api/propose", {"stint": stint})
    assert pr["brake_pressure_by_feel"] is False  # not a prototype: no button
    cls = pr["car_class"]
    assert pr["feel"] == {"front": None, "rear": None}
    assert call(base, "/api/feel", {"car_class": cls, "front": 2})[0] == 400
    assert call(base, "/api/feel", {"car_class": cls, "front": -1})[1]["feel"] == {"front": -1, "rear": None}
    assert call(base, "/api/feel", {"car_class": cls, "rear": 1})[1]["feel"] == {"front": -1, "rear": 1}  # each on its own
    assert call(base, "/api/proposal?stint=%d" % stint)[1]["feel"] == {"front": -1, "rear": 1}
    assert call(base, "/api/feel", {"car_class": cls, "front": None, "rear": None})[1]["feel"] == {"front": None, "rear": None}
    # The speed answers are stored with the stint like the phase ratings.
    _, r = call(base, "/api/rating", {"file_name": name, "fast": -1})
    assert (r["rating"]["fast"], r["rating"]["mid"]) == (-1, 1)
    assert call(base, "/api/rating", {"file_name": name, "slow": 3})[0] == 400
    call(base, "/api/rating", {"file_name": name, "fast": None})
    # Zones (corner speed × phase) in half points, kept with the stint's other ratings.
    _, r = call(base, "/api/rating", {"file_name": name, "slow_exit": -1.5})
    assert r["rating"]["slow_exit"] == -1.5 and r["rating"]["mid"] == 1
    assert call(base, "/api/rating", {"file_name": name, "slow_exit": -1.25})[0] == 400
    _, r = call(base, "/api/rating", {"file_name": name, "slow_exit": None})
    assert "slow_exit" not in r["rating"]
    [c] = pr["changes"]
    assert c["keys"] == ["WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"] and c["delta"] == 2  # rake first, ~2 mm
    assert pr["target"] == {"file": "autosetup_TestTeam1_Testway_1.svm", "folder": "Testway"}

    # A stale signature is refused; the right one writes into the track's folder.
    assert call(base, "/api/write", {"stint": stint, "signature": "stale"})[0] == 409
    status, w = call(base, "/api/write", {"stint": stint, "signature": pr["signature"]})
    assert status == 200 and w["file"] == "autosetup_TestTeam1_Testway_1.svm"
    written = install.settings / "Testway" / w["file"]
    assert written.is_file()
    assert app.advisor.written(stint)[0]["file"] == w["file"]

    # Locking the rear ride height moves the advice to the front bar.
    rear = ["WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"]
    status, lk = call(base, "/api/locks", {"stint": stint, "keys": rear, "locked": True})
    assert status == 200 and lk["locks"] == rear
    _, pr = call(base, f"/api/proposal?stint={stint}")
    assert pr["changes"][0]["keys"] == ["VM_FRONT_ANTISWAY"]
    assert pr["locks"] == [{"key": "WM_RIDEHEIGHT-W_RL", "name": "Ride height RL"},
                           {"key": "WM_RIDEHEIGHT-W_RR", "name": "Ride height RR"}]
    call(base, "/api/locks", {"stint": stint, "keys": rear, "locked": False})
    assert call(base, f"/api/proposal?stint={stint}")[1]["locks"] == []
    assert call(base, "/api/locks", {"stint": stint, "keys": "VM_X"})[0] == 400
    assert call(base, "/api/proposal?stint=999")[0] == 400


def test_model_learns_then_takes_over_balance(tmp_path, install):
    """Store-backed: one qualifying stint → learning (one click); four stints on three
    setups → ready, with the model's state in the proposal."""
    from autosetup.proposals import MODEL_CONFIDENCE, Advisor

    with Store(tmp_path / "db.duckdb") as store:
        adv = Advisor(store, install)
        first = make_recording(install.telemetry / "a.duckdb", laps=4)
        store.import_recording(first)
        rid = store.con.execute("select id from recordings where file_name = 'a.duckdb'").fetchone()[0]
        store.put_setting("change_size", "small")
        pr = adv.proposal(rid)
        assert pr.model["state"] == "learning" and pr.model["n_obs"] == 1
        [c] = [c for c in pr.changes if c.group == "balance"]
        assert c.source == "learning" and abs(c.delta) == 1 and c.reason.startswith("Learning (1 stint")
        store.put_setting("change_size", "large")
        moves = [c for c in adv.proposal(rid).changes if c.source == "learning"]
        assert 1 < len(moves) <= 5

        for i, rear in enumerate((2, 4, 3)):
            store.import_recording(make_recording(install.telemetry / f"b{i}.duckdb", laps=4,
                                                  values={"VM_REAR_ANTISWAY": rear}))
        last = store.con.execute("select max(id) from recordings").fetchone()[0]
        pr = adv.proposal(last)
        assert pr.model["state"] == "ready" and pr.model["n_obs"] == 4 and pr.model["n_setups"] >= 3
        assert pr.model["effects"] and pr.model["experience_effect"] is not None
        # Four look-alike stints: enough to be "ready", not enough to be sure of anything, so
        # the rules keep the balance and the model only learns.
        assert pr.model["confidence"] is not None and pr.model["exploring"] == (pr.model["confidence"] < MODEL_CONFIDENCE)
        sources = ("learning",) if pr.model["exploring"] else ("model",)
        assert all(c.source in (*sources, "rules", "rating", "telemetry") for c in pr.changes)


def test_change_size_setting(server):
    _, base = server
    assert call(base, "/api/state")[1]["change_size"] == "large"
    assert call(base, "/api/change-size", {"size": "small"}) == (200, {"change_size": "small"})
    assert call(base, "/api/state")[1]["change_size"] == "small"
    assert call(base, "/api/change-size", {"size": "huge"})[0] == 400


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
        for id_ in (b'id="menu-btn"', b"screen-driver", b"screen-settings", b'id="keys"', b'id="about"'):
            assert id_ in page, id_
    call(base, "/api/import", {})
    _, d = call(base, "/api/driver")
    [c] = d["classes"]
    assert c["laps"] > 0 and c["rotation"] == [] and set(d["tradeoff"]) == {"pace", "drivability", "tyres"}
    _, a = call(base, "/api/about")
    assert a["recordings"] == 1 and a["laps"] > 0 and a["database"].endswith("db.duckdb")

    _, s = call(base, "/api/settings")
    assert s["units"] == {"speed": "km/h", "temp": "°C", "pressure": "kPa"}
    assert [f["which"] for f in s["folders"]] == ["install", "telemetry", "settings", "database"]
    assert [x["car_class"] for x in s["classes"]] == [c["car_class"]]
    _, s = call(base, "/api/settings", {"units": {"temp": "°F"}, "change_size": "small"})
    assert s["units"]["temp"] == "°F" and s["units"]["speed"] == "km/h" and s["change_size"] == "small"
    assert call(base, "/api/settings", {"change_size": "huge"})[0] == 400
    _, s = call(base, "/api/settings", {"reset_class": c["car_class"]})
    assert s["classes"][0]["reset_at"]
    assert call(base, "/api/open-folder", {"which": "nowhere"})[0] == 400


def test_units_convert_our_numbers():
    from autosetup.units import Units

    f = Units.from_json({"temp": "°F", "pressure": "psi", "speed": "nonsense"})
    assert f.speed == "km/h"  # unknown choices fall back
    assert f.fmt_temp(100, 0) == "212 °F" and f.fmt_temp(10, 0, delta=True) == "18 °F"  # a difference has no offset
    assert f.fmt_pressure(172) == "24.9 psi"
    assert Units().fmt_temp(4.94, delta=True) == "4.9 °C"
