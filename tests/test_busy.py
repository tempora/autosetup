"""LMU writes an outing through a DuckDB write-ahead log (``<file>.duckdb.wal``) and keeps
the files locked until you return to the garage."""

import shutil
import time

import duckdb
import pytest

from autosetup.lmu import telemetry
from autosetup.lmu.telemetry import RecordingBusy, Session, in_progress, wal_of
from autosetup.server import App
from autosetup.store import Store
from tests.conftest import make_recording


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(telemetry.time, "sleep", lambda s: None)


def test_lock_is_retried_then_reported_busy(recording, monkeypatch, no_sleep):
    calls = {"n": 0}
    real_copy = shutil.copyfile

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "Permission denied", str(src))
        return real_copy(src, dst)

    monkeypatch.setattr(telemetry.shutil, "copyfile", flaky)
    with Session.open(recording) as s:  # third attempt succeeds
        assert s.track == "Test Raceway"
    assert calls["n"] == 3

    def locked(src, dst):
        raise PermissionError(13, "Permission denied", str(src))

    monkeypatch.setattr(telemetry.shutil, "copyfile", locked)
    with pytest.raises(RecordingBusy):
        Session.open(recording)


def _leave_wal(path):
    """Write to a recording without checkpointing, the way LMU leaves it mid-outing:
    one more lap event lands in ``<file>.wal``."""
    con = duckdb.connect(str(path))
    con.execute("PRAGMA disable_checkpoint_on_shutdown")
    con.execute('insert into "Lap" values (?, ?)', [999999.0, 42])
    con.close()
    assert wal_of(path).exists()
    return path


def test_outing_with_a_log_waits_while_the_game_runs(tmp_path):
    rec = _leave_wal(make_recording(tmp_path / "live.duckdb"))
    assert in_progress(rec)
    with Store(tmp_path / "db.duckdb") as store:
        with pytest.raises(RecordingBusy):
            store.import_recording(rec, only_finished=True, game_running=True)


def test_leftover_log_is_replayed_when_the_game_is_gone(tmp_path):
    rec = _leave_wal(make_recording(tmp_path / "crashed.duckdb"))
    with Session.open(rec) as s:
        ts, laps = s.events("Lap")
        assert laps[-1] == 42  # the lap only the log knew about
    assert wal_of(rec).exists()  # LMU's files untouched
    with Store(tmp_path / "db.duckdb") as store:
        assert store.import_recording(rec, only_finished=True, game_running=False) is not None


def test_finished_outing_imports_even_if_the_car_never_drove_into_the_pits(tmp_path):
    """Returning to the garage from the track (no pit-lane entry) still closes the file."""
    rec = make_recording(tmp_path / "teleported.duckdb")
    con = duckdb.connect(str(rec))
    con.execute('delete from "In Pits" where value = 1 and ts > (select min(ts) from "In Pits")')
    con.close()
    assert not in_progress(rec)
    with Store(tmp_path / "db.duckdb") as store:
        assert store.import_recording(rec, only_finished=True, game_running=True) is not None


def test_watcher_reports_waiting_not_error_and_clears_old_errors(tmp_path, install, no_sleep):
    live = install.telemetry / "Test Raceway_P_2026-01-01T10_00_00Z.duckdb"
    _leave_wal(live)
    with Store(tmp_path / "db.duckdb") as store:
        app = App(store, install)
        app.game_running = lambda: True
        app.status["last_error"] = "old: PermissionError"
        now = time.time()
        app.poll(now=now)
        assert app.poll(now=now + 11) == 0
        assert app.status["waiting"] == [live.name]
        assert app.status["last_error"] is None
        # Back in the garage: LMU merges the log and deletes it.
        con = duckdb.connect(str(live))
        con.execute("checkpoint")
        con.close()
        assert not in_progress(live)
        # The log going is the signal: imported on the next poll, no settling wait.
        assert app.poll(now=now + 12) == 1
        assert app.status["waiting"] == [] and app.status["last_error"] is None


def test_cli_import_lists_busy_files(tmp_path, install):
    _leave_wal(install.telemetry / "Test Raceway_P_2026-01-01T10_00_00Z.duckdb")
    with Store(tmp_path / "db.duckdb") as store:
        added, errors, busy = store.import_all(install.telemetry, game_running=True)
    assert added == [] and errors == [] and [f.name for f in busy] == ["Test Raceway_P_2026-01-01T10_00_00Z.duckdb"]
