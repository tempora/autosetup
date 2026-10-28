"""Aero calibration: synthetic laps from the lap sim with known drag and downforce, and
recorded Interlagos stints (current build) that differ in rear wing."""

from types import SimpleNamespace

import numpy as np
import pytest

from autosetup.lmu.telemetry import Lap
from autosetup.sim.aero import AeroMap, Platform, Stint, drag_deltas, envelope_car, lift_deltas, lift_of, shrink
from autosetup.sim.extract import extract
from autosetup.sim.lapsim import G, Track, fit_eta, simulate
from tests.test_balance_metric import make_signals
from tests.test_lapsim import toy_car, track

NO_EVENTS = SimpleNamespace(tables=set())  # a Session without Gear / Current Sector


def drive_laps(t: Track, car, laps: int = 3, slope: np.ndarray | None = None, seed: int = 0):
    """Signals of ``laps`` laps driven exactly as the sim says, on the 50 Hz grid; an
    optional per-step slope (m/s²) adds to the measured longitudinal acceleration."""
    res = simulate(t, car)
    rng = np.random.default_rng(seed)
    d_step = np.arange(len(t.kappa)) * t.step
    t_lap = res.time
    tt = np.arange(0, laps * t_lap, 0.02)
    tl = tt % t_lap
    d = np.interp(tl, res.t, d_step)
    v = np.interp(d, d_step, res.v)
    kappa = np.interp(d, d_step, t.kappa)
    a_lon = np.gradient(v, 0.02)
    if slope is not None:
        a_lon = a_lon + np.interp(d, d_step, slope)
    a_lon = a_lon + rng.normal(0, 0.05, len(tt))
    drive_lim = np.array([car.at(car.drive, x) for x in v])
    throttle = np.where(a_lon > 0.6 * drive_lim - 0.3, 1.0, 0.0)
    brake = np.where(a_lon < -2.0, 0.8, 0.0)
    sig = make_signals(len(tt), t=tt, dist=d, speed=v, a_lat=v ** 2 * kappa / G, a_lon=a_lon / G,
                       yaw_rate=v * kappa, throttle=throttle, brake=brake,
                       ride_height=np.full((len(tt), 4), 0.05))
    lap_list = [Lap(k + 1, k * t_lap, (k + 1) * t_lap, True, False) for k in range(laps)]
    return sig, lap_list, res


CIRCUIT = track((900, 0), (150, 45), (400, 0), (250, 120), (700, 0), (300, 300), (200, 0), (120, 35),
                (500, 0), (200, 90))


def test_measured_envelope_reproduces_the_lap():
    """Extract → envelope car → sim gives back the lap that produced the data."""
    car = toy_car()
    sig, laps, res = drive_laps(CIRCUIT, car)
    sd = extract(NO_EVENTS, sig, laps, {0, 1, 2})
    fitted = envelope_car(sd.envelope, 6e-5)
    assert fitted is not None and sd.profile is not None
    assert simulate(Track(5.0, sd.profile.kappa), fitted).time == pytest.approx(res.time, rel=0.02)
    for v in (35.0, 50.0, 65.0):  # the drive curve where the straights measured it
        assert fitted.at(fitted.drive, v) == pytest.approx(car.at(car.drive, v), abs=0.5)


def test_drag_difference_between_stints_is_recovered_on_a_hilly_track():
    """Two stints, the second with 1.2e-4 more drag, on a track with a hill (it would read
    as drag if it weren't shared)."""
    hill = np.where((np.arange(len(CIRCUIT.kappa)) * 5.0 > 100) & (np.arange(len(CIRCUIT.kappa)) * 5.0 < 700), -0.8, 0.0)
    rows = []
    for rid, dk in ((1, 0.0), (2, 1.2e-4)):
        sig, laps, _ = drive_laps(CIRCUIT, toy_car().perturb(d_drag=dk), slope=hill, seed=rid)
        rows += [(rid, b, n, v, a) for _, b, n, v, a in extract(NO_EVENTS, sig, laps, {0, 1, 2}).straights]
    out = drag_deltas(rows)
    assert out[1][0] + out[2][0] == pytest.approx(0.0, abs=1e-12)
    diff = out[2][0] - out[1][0]
    assert diff == pytest.approx(1.2e-4, rel=0.25)
    assert diff > 4 * np.hypot(out[1][1], out[2][1])


def test_downforce_difference_between_stints_is_recovered():
    """Two stints, the second with 5e-5 more downforce, on a circuit whose fastest corner
    is flat out: compared bin by bin with the other stint, the difference shows, biased
    towards zero (bins where neither car is at the limit dilute it: conservative, never
    the wrong way); an absolute fit of either stint alone reads the flat-out corner as
    less downforce."""
    envs = {}
    for rid, lift in ((1, 4e-5), (2, 9e-5)):
        sig, laps, _ = drive_laps(CIRCUIT, toy_car(lift=lift), seed=rid)
        envs[rid] = extract(NO_EVENTS, sig, laps, {0, 1, 2}).envelope
    out = lift_deltas(envs, mu=1.6)
    diff = out[2][0] - out[1][0]
    assert 0.25 * 5e-5 < diff < 1.2 * 5e-5 and diff > 2 * np.hypot(out[1][1], out[2][1])
    assert lift_of(envs[2])[0] < 9e-5 / 2  # absolute: biased low


def test_shrink_pulls_noisy_stints_to_the_pool():
    out = shrink({1: (1.0, 0.01), 2: (3.0, 0.01), 3: (10.0, 100.0)}, prior_sd=1.0)
    assert abs(out[3]) < 0.01 and out[2] - out[1] == pytest.approx(2.0, rel=0.01)


def test_aero_map_learns_slopes_across_layouts():
    """Stints at two layouts, each with its own level: the map learns the slopes they share."""
    k = {"aero_prior_sd": 1.0, "drag_k_typ": 7e-4, "lift_c_typ": 6e-5,
         **{f"aero_{q}_per_{f}": 0.0 for q in ("drag", "lift", "balance") for f in ("wing", "mm_rh", "mm_rake")}}
    rng = np.random.default_rng(3)
    stints = []
    for layout, level in (("A", 0.0), ("B", 3e-5)):
        for _ in range(5):
            p = Platform(rng.uniform(0, 1), rng.uniform(35, 45), rng.uniform(45, 60))
            drag = level + 8e-5 * p.wing + 1e-6 * p.rake + rng.normal(0, 2e-6)
            bal = 0.6 * p.wing - 0.03 * p.rake + rng.normal(0, 0.05)
            stints.append(Stint(layout, p, (drag, 2e-6), None, (bal, 0.05)))
    amap = AeroMap.fit(stints, k)
    sd = lambda m, i: float(np.sqrt(m.cov[i, i]))  # noqa: E731
    assert abs(amap.drag.mean[0] - 8e-5) < 3 * sd(amap.drag, 0)
    assert abs(amap.balance.mean[0] - 0.6) < 3 * sd(amap.balance, 0)
    assert abs(amap.balance.mean[2] + 0.03) < 3 * sd(amap.balance, 2) and amap.balance.mean[2] < 0
    assert amap.n["lift"] == 0 and np.allclose(amap.lift.mean, 0.0)  # no evidence: the prior


def test_driver_efficiency_is_shared_not_per_setup():
    """η fitted over several stints can't absorb a setup's effect: the faster car stays faster."""
    slow, quick = toy_car(), toy_car(lift=9e-5)
    times = [(CIRCUIT, c, simulate(CIRCUIT, c.scaled(0.92)).time) for c in (slow, quick)]
    eta = fit_eta(times)
    assert eta == pytest.approx(0.92, abs=0.01)
    assert simulate(CIRCUIT, quick.scaled(eta)).time < simulate(CIRCUIT, slow.scaled(eta)).time


# -- recorded --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def interlagos(recorded):
    from autosetup.store import read_recording

    return {n: read_recording(recorded(n)) for n in ("interlagos_wing_11", "interlagos_wing_8",
                                                     "interlagos_arb_r3", "interlagos_arb_r5")}


def test_more_rear_wing_reads_more_drag(interlagos):
    """Rear wing 8.3° < 11.3° < 12.3° (two stints at 12.3°): drag in the same order."""
    names = list(interlagos)
    rows = [(i, b, n, v, a) for i, name in enumerate(names) for _, b, n, v, a in interlagos[name].obs.sim.straights]
    dk = drag_deltas(rows)
    wing_8, wing_11, wing_12a, wing_12b = (dk[names.index(n)] for n in
                                           ("interlagos_wing_8", "interlagos_wing_11", "interlagos_arb_r3", "interlagos_arb_r5"))
    assert wing_8[0] < wing_11[0] < min(wing_12a[0], wing_12b[0])
    assert wing_11[0] - wing_8[0] > 3 * np.hypot(wing_8[1], wing_11[1])


def test_sim_predicts_a_stint_from_the_others(interlagos):
    """Leave one out: the car pooled from three stints, η fitted on them, predicts the
    fourth stint's best lap within 1.5 %."""
    from autosetup.sim.aero import layout_car

    names = list(interlagos)
    for held in names:
        others = [n for n in names if n != held]
        base = layout_car([interlagos[n].obs.sim.envelope for n in others], 6e-5)
        prof = {n: interlagos[n].obs.sim.profile for n in names}
        eta = fit_eta([(Track(5.0, prof[n].kappa), base, prof[n].lap_time) for n in others])
        pred = simulate(Track(5.0, prof[held].kappa), base.scaled(eta)).time
        assert pred == pytest.approx(prof[held].lap_time, rel=0.015), held


def test_residual_gp_learns_what_the_sim_misses():
    """Sector residuals that depend on a setting (e.g. mechanical grip the point mass
    doesn't see) are learned and predicted where the setting wasn't tried."""
    from autosetup.sim.residual import SectorResidual

    rng = np.random.default_rng(0)
    X = rng.uniform(0, 1, (8, 2))
    truth = np.column_stack([0.3 * X[:, 0], -0.2 * X[:, 1], 0.1 + 0 * X[:, 0]])
    res = SectorResidual.fit(X, truth + rng.normal(0, 0.02, truth.shape), np.full(truth.shape, 0.02 ** 2))
    m, v = res.predict(np.array([[0.5, 0.5]]))
    assert m[0] == pytest.approx([0.15, -0.1, 0.1], abs=0.06)
    lap_m, lap_v = res.lap(np.array([[0.5, 0.5]]))
    assert lap_m[0] == pytest.approx(m[0].sum()) and lap_v[0] > 0


def test_top_speeds_correct_a_stint_drag_estimate():
    """A stint whose straights were misread (drag estimate too low) is pulled back by its
    measured top speeds against the other stints."""
    from autosetup.sim.extract import straights_of
    from autosetup.sim.model import LayoutModel, StintData
    from autosetup.sim.residual import trap_corrections

    t = Track(CIRCUIT.step, CIRCUIT.kappa, (), tuple(d1 for _, d1 in straights_of(CIRCUIT.kappa)))
    base = toy_car()
    true_dk = {1: -5e-5, 2: 0.0, 3: 5e-5}
    stints = []
    for rid, dk in true_dk.items():
        res = simulate(t, base.perturb(d_drag=dk))
        traps = [(0, d, v) for d, v in zip(t.traps, res.traps)]
        stints.append(StintData(rid, f"{rid}", None, None, [], [], traps, (t.kappa, res.v, res.time, []), [0]))
    misread = {1: (-5e-5, 1e-5), 2: (0.0, 1e-5), 3: (0.0, 1e-5)}  # stint 3's straights said nothing
    model = LayoutModel("sig", "L", t, base, 1.0, None, misread, {}, stints)
    corr = trap_corrections(model)
    assert corr[3][0] > 2e-5 and abs(corr[2][0]) < corr[3][0] and corr[1][0] < corr[3][0]


def test_layout_model_and_aero_map_from_the_store(recorded, tmp_path):
    """Import the Interlagos stints, build the layout model (current build only) and the
    car's aero map; the map is kept and reused until a stint is added."""
    from autosetup.knowledge import load_knowledge
    from autosetup.sim.model import LayoutModel, aero_map
    from autosetup.store import Store

    store = Store(tmp_path / "db.duckdb")
    for n in ("interlagos_wing_11", "interlagos_wing_8", "interlagos_arb_r3"):
        store.import_recording(recorded(n))
    sig, layout = store.con.execute("select car_signature, coalesce(track_layout, track) from recordings limit 1").fetchone()
    k = load_knowledge("GT3")
    model = LayoutModel.build(store, sig, layout, k)
    assert model is not None and len(model.stints) == 3 and 0.8 < model.eta < 1.0
    for s in model.stints:
        if s.profile is not None:
            assert model.simulate(model.stint_car(s.rid)).time == pytest.approx(s.profile[2], rel=0.02)
    amap = aero_map(store, sig, k)
    assert amap.drag.mean[0] > 0  # more rear wing, more drag
    assert store.con.execute("select count(*) from aero_maps").fetchone()[0] == 1
    assert aero_map(store, sig, k).to_json() == amap.to_json()
    store.import_recording(recorded("interlagos_arb_r5"))
    assert aero_map(store, sig, k).n["drag"] == 4  # refitted with the new stint
