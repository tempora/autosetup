"""Checks against the local LMU install. Opt-in because they read every recording and
saved setup: ``pytest -m install``. Skipped when the game isn't found."""

import pytest

from autosetup.lmu.mapping import DO_NOT_WRITE
from autosetup.lmu.paths import find_install
from autosetup.lmu.telemetry import Session, in_progress, list_recordings
from autosetup.lmu.validate import validate

try:
    PATHS = find_install()
except FileNotFoundError:
    PATHS = None

pytestmark = [pytest.mark.install,
              pytest.mark.skipif(PATHS is None, reason="Le Mans Ultimate install not found")]


def test_mapping_agrees_with_saved_setups():
    report = validate(PATHS.telemetry, PATHS.settings)
    if not report.matches:
        pytest.skip("no recording matches a saved setup")
    assert not report.unmapped_setup_keys
    assert set(report.suspects()) <= DO_NOT_WRITE


def test_lap_events_match_lap_distance_wraps():
    import numpy as np

    checked = 0
    for rec in list_recordings(PATHS.telemetry):
        if in_progress(rec):  # LMU is writing it right now
            continue
        with Session.open(rec) as s:
            if "Lap Dist" not in s.frequencies or s.duration < 120:
                continue
            t, dist = s.channel("Lap Dist")
            wraps = t[1:][np.diff(dist) < -500]
            crossings = s.events("Lap")[0][1:]
            if not len(crossings):
                continue
            nearest = np.abs(wraps[:, None] - crossings[None, :]).min(axis=0)
            assert np.all(nearest < 0.25), (rec.name, nearest)
            checked += 1
    assert checked


def test_pipeline_on_every_current_build_recording(tmp_path):
    """Corner passes, suspension and kerbs from each recording of the installed game build
    with a lap at speed (older builds are never learned from)."""
    from autosetup.lmu.builds import current_filter, note_build
    from autosetup.store import Store

    store = Store(tmp_path / "db.duckdb")
    store.import_all(PATHS.telemetry)
    note_build(store, PATHS)
    cond, params = current_filter(store)
    rows = store.con.execute(
        f"""select r.file_name, (select count(*) from corner_passes p where p.recording_id = r.id),
                   (select count(*) from lap_suspension s where s.recording_id = r.id)
            from recordings r where {cond} and exists (select 1 from laps l where l.recording_id = r.id
              and not l.in_pits and l.lap_time > 60)""", params).fetchall()
    assert rows
    for name, n_passes, n_susp in rows:
        assert n_passes > 0 and n_susp > 0, name
