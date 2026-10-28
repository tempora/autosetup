"""The pipeline on recorded telemetry (tests/data: current game build, Interlagos GT3,
stints that differ in one setting)."""

import numpy as np
import pytest

from autosetup.metrics.balance import cells, normalise
from autosetup.store import read_recording


@pytest.fixture(scope="module")
def read(recorded):
    cache = {}

    def get(name):
        if name not in cache:
            cache[name] = read_recording(recorded(name))
        return cache[name]

    return get


def _cells(rec):
    return cells(normalise([o for _, o in rec.obs.passes]))


def test_corner_passes_cover_every_lap_and_phase(read):
    rec = read("interlagos_wing_11")
    assert 8 <= len(rec.obs.corners) <= 15
    laps = {seq for seq, _ in rec.obs.passes}
    assert len(laps) >= 3  # partial laps outside the pits count too
    assert {o.phase for _, o in rec.obs.passes} == {"entry", "mid", "exit"}
    assert {o.band for _, o in rec.obs.passes} == {"low", "high"}


def test_kerbs_are_tagged_in_the_same_places_every_lap(read):
    """Kerb events from the surface codes recur at the same lap distances on each lap."""
    rec = read("interlagos_wing_11")
    assert rec.obs.kerb_source == "surface"
    complete = [i for i, lap in enumerate(rec.laps) if lap.complete and not lap.in_pits]
    spots = [sorted(e.dist for seq, e in rec.obs.kerbs if seq == i) for i in complete]
    assert len(complete) >= 3 and all(len(s) >= 4 for s in spots)
    every_lap = [d for d in spots[0] if all(any(abs(d - x) < 30 for x in s) for s in spots[1:])]
    assert len(every_lap) >= 4


def test_suspension_histograms_are_plausible(read):
    rec = read("interlagos_wing_11")
    off = [s for s in rec.obs.suspension if not s.kerb]
    on = [s for s in rec.obs.suspension if s.kerb]
    assert {s.wheel for s in off} == {"fl", "fr", "rl", "rr"} and on
    for s in off:
        assert 0.4 < s.stats["bump_frac"] < 0.6
        assert 0.05 < s.stats["load_cv"] < 0.6
    # Kerbs move the dampers faster than the rest of the lap.
    assert np.mean([s.stats["ls_frac"] for s in on]) < np.mean([s.stats["ls_frac"] for s in off])


def test_stiffer_rear_bar_reads_less_exit_understeer(read):
    """Rear anti-roll bar P3 → P5, nothing else changed: low-speed exits rotate more."""
    soft = _cells(read("interlagos_arb_r3"))[("exit", "low")]
    stiff = _cells(read("interlagos_arb_r5"))[("exit", "low")]
    assert soft.mean - stiff.mean > 1.5 * np.hypot(soft.se, stiff.se)


def test_less_rear_wing_reads_less_understeer(read):
    """Rear wing 11.3° → 8.3°, nothing else changed: every phase × speed band measured in
    both stints rotates more, and together clearly so (the 8.3° stint is one lap)."""
    more, less = _cells(read("interlagos_wing_11")), _cells(read("interlagos_wing_8"))
    shared = sorted(set(more) & set(less))
    z = [(more[k].mean - less[k].mean) / np.hypot(more[k].se, less[k].se) for k in shared]
    assert len(shared) >= 3 and all(x > 0 for x in z)
    assert sum(z) / np.sqrt(len(z)) > 1.5  # Stouffer


def test_store_keeps_one_corner_map_per_layout(recorded, tmp_path):
    """Two stints at the same layout share corner ids; re-importing replaces, not adds."""
    from autosetup.store import Store

    store = Store(tmp_path / "db.duckdb")
    a = store.import_recording(recorded("interlagos_arb_r3"))
    b = store.import_recording(recorded("interlagos_arb_r5"))
    layouts = store.con.execute("select layout, count(*) from corners group by 1").fetchall()
    assert len(layouts) == 1 and layouts[0][1] >= 8
    ids = {rid: {c for (c,) in store.con.execute(
        "select distinct corner from corner_passes where recording_id = ?", [rid]).fetchall()} for rid in (a, b)}
    assert len(ids[a] & ids[b]) >= 0.8 * len(ids[a] | ids[b])
    n = store.con.execute("select count(*) from corner_passes").fetchone()[0]
    store._delete_recording(b)
    store.save_recording(read_recording(recorded("interlagos_arb_r5")))
    assert store.con.execute("select count(*) from corner_passes").fetchone()[0] == n
    hist_lo, hist = store.con.execute("select hist_lo, hist from lap_suspension limit 1").fetchone()
    assert hist_lo >= 0 and len(__import__("json").loads(hist)) > 0


def test_a_matching_dam_log_supplies_tyre_load_and_slip(recorded, tmp_path):
    """With a DAM log of the outing, wheel load is the logged tyre load (not the spring
    proxy) and each pass gets front-minus-rear slip."""
    from autosetup.lmu import motec
    from autosetup.lmu.setup_space import CarSetup
    from autosetup.lmu.telemetry import Session
    from autosetup.metrics.observations import observe_recording
    from autosetup.metrics.signals import load_signals
    from tests.test_motec import write_ld

    with Session.open(recorded("interlagos_arb_r5")) as s:
        t, speed = s.channel("Ground Speed")
        t10 = np.arange(s.t_start, s.t_end, 0.1)
        v10 = np.interp(t10, t, speed)
        load = 3500 + 800 * np.sin(t10 * 3.0)
        lat = 0.03 * v10 / 3.6 * np.sin(t10 / 4.0)
        chans = {motec.CLOCK: (10, np.floor(t10 + 0.4), "s"), "Ground Speed": (10, v10, "km/h")}
        for w in motec.WHEELS:
            chans[f"Tyre Load {w}"] = (10, load, "N")
            chans[f"Lat Ground Vel {w}"] = (10, lat * (1.2 if w[0] == "F" else 1.0), "m/s")
            chans[f"Long Ground Vel {w}"] = (10, v10 / 3.6, "m/s")
        write_ld(tmp_path / "dam.ld", chans, venue=s.track)
        log = motec.MotecLog(tmp_path / "dam.ld")
        sig = load_signals(s)
        obs = observe_recording(s, sig, s.laps(), CarSetup.from_json(s.setup_json), log=log)
    assert obs.dam == "dam.ld" and obs.load_source == "tyre_load"
    cv = [x.stats["load_cv"] for x in obs.suspension if not x.kerb]
    assert np.allclose(cv, 800 / np.sqrt(2) / 3500, atol=0.03)
    slips = [o.slip_fr for _, o in obs.passes if o.slip_fr is not None]
    assert len(slips) > 0.8 * len(obs.passes) and np.median(slips) > 0  # the fronts slide more
