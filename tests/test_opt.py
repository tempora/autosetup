import numpy as np
import pytest

from autosetup.opt import bench
from autosetup.opt.dataset import Dataset, Observation, _correct, _within_stint_trend, build_dims
from autosetup.opt.gp import GP
from autosetup.opt.optimizer import CHANGE_SIZES, RADIUS_MAX, RADIUS_MIN, group_of, next_radius, optimise

W = {"pace": 1.0, "tyres": 0.5, "drivability": 0.75}


# -- GP ----------------------------------------------------------------------------------
def test_gp_fits_a_smooth_function_and_respects_noise():
    rng = np.random.default_rng(0)
    X = rng.uniform(0, 1, (30, 2))
    f = lambda x: np.sin(3 * x[:, 0]) + 0.5 * x[:, 1]
    gp = GP.fit(X, f(X) + rng.normal(0, 0.05, 30), np.full(30, 0.05 ** 2), prior_spread=0.5)
    Xs = rng.uniform(0.1, 0.9, (20, 2))
    mu, var = gp.predict(Xs)
    assert np.abs(mu - f(Xs)).max() < 0.15
    assert (np.sqrt(var) < 0.15).all()
    far_mu, far_var = gp.predict(np.array([[5.0, 5.0]]))
    assert np.sqrt(far_var[0]) > 0.3  # uncertain away from data


def test_gp_with_two_points_doesnt_collapse():
    X = np.array([[0.2] * 5, [0.2] * 4 + [0.8]])
    gp = GP.fit(X, np.array([64.2, 63.8]), np.array([0.01, 0.01]), prior_spread=0.25)
    assert gp.lengthscale >= 0.45  # the prior keeps effects smooth
    mu, _ = gp.predict(X)
    assert mu[0] > mu[1]  # it still follows the data


# -- corrections -------------------------------------------------------------------------
def _laps(rid, times, fuel0=40.0, burn=2.0):
    return [dict(rid=rid, time=t, clean=True, stint_lap=i + 1, fuel=fuel0 - burn * i) for i, t in enumerate(times)]


def test_within_stint_trend_is_fitted_on_demeaned_laps():
    # Two stints at very different pace levels, both slowing 0.1 s/lap after fuel.
    fuel = 0.03
    a = _laps(1, [60 + 0.1 * i + fuel * (40 - 2 * i) for i in range(30)])
    b = _laps(2, [70 + 0.1 * i + fuel * (40 - 2 * i) for i in range(30)])
    slope, sigma = _within_stint_trend(a + b, fuel)
    assert slope == pytest.approx(0.1 * 9000 / (9000 + 20), rel=0.01)  # ridge shrinks a little
    assert sigma < 0.01


def test_correct_moves_laps_to_reference_fuel_and_stint_lap():
    lap = dict(time=64.0, fuel=50.0, stint_lap=7)
    assert _correct(lap, 0.03, 0.05, ref_fuel=30.0) == pytest.approx(64.0 - 0.6 - 0.2)


# -- optimiser ---------------------------------------------------------------------------
def _obs(values, pace, i, experience=0.0, n=8):
    setup = bench.make_setup({**bench.start_values(), **values})
    y = {"pace": pace, "tyres": 0.5, "drivability": 0.2}
    noise = {"pace": 0.25 ** 2 / n, "tyres": 1e-4, "drivability": 1e-3}
    return Observation(i, f"s{i}", setup, y, noise, experience, 30.0, n)


def _ds(obs):
    return Dataset("toy", build_dims(obs[-1].setup), obs, 0.0, 0.0, 0.25)


def test_learning_small_is_one_click():
    ds = _ds([_obs({}, 100.0, 0)])
    res = optimise(ds, ds.obs[-1].setup, W, (8.0, 30.0), size="small", learning=True)
    assert len(res.moves) == 1 and abs(res.moves[0].steps) == 1


def test_learning_large_changes_several_groups_by_real_steps():
    ds = _ds([_obs({}, 100.0, 0)])
    res = optimise(ds, ds.obs[-1].setup, W, (8.0, 30.0), size="large", learning=True)
    assert len(res.moves) == CHANGE_SIZES["large"].moves
    assert len({group_of(m.dim) for m in res.moves}) == len(res.moves)  # spread over the car
    for m in res.moves:
        expected = max(1, round(0.15 * (m.dim.hi - m.dim.lo)))
        assert abs(m.steps) == expected


def test_locks_are_respected():
    ds = _ds([_obs({}, 100.0, 0)])
    locked = {k for k in ds.obs[-1].setup.params if k != "VM_REAR_WING"}
    res = optimise(ds, ds.obs[-1].setup, W, (8.0, 30.0), locks=locked, size="small", learning=True)
    assert [m.dim.name for m in res.moves] == ["VM_REAR_WING"]


def test_moves_the_setting_that_clearly_helps():
    # Lower rear wing has been faster every time; nothing else changed.
    obs = [_obs({"VM_REAR_WING": w}, 100.0 + 0.15 * w, i, experience=50.0) for i, w in enumerate((8, 6, 4, 6, 8, 4))]
    ds = _ds(obs)
    base = _obs({"VM_REAR_WING": 6}, 0, 99).setup
    res = optimise(ds, base, {"pace": 1.0, "tyres": 0.0, "drivability": 0.0}, (50.0, 30.0), size="small")
    assert res.moves and res.moves[0].dim.name == "VM_REAR_WING" and res.moves[0].steps < 0
    assert res.predicted["pace"][0] < 0
    wing = next(e for e in res.effects if e["dim"] == "VM_REAR_WING")
    assert wing["delta"]["pace"][0] > 0.05  # one step more wing costs time


def test_confidence_tells_knowing_from_guessing():
    # Six setups, lap times all inside the noise: EI still finds a move, but the model
    # can't claim it beats the current setup.
    rng = np.random.default_rng(1)
    flat = [_obs({"VM_REAR_WING": w, "VM_FRONT_ANTISWAY": b}, 100.0 + rng.normal(0, 0.05), i, experience=50.0)
            for i, (w, b) in enumerate(((8, 3), (6, 5), (4, 4), (6, 2), (8, 5), (4, 3)))]
    guess = optimise(_ds(flat), flat[-1].setup, W, (50.0, 30.0), size="large")
    clear = [_obs({"VM_REAR_WING": w}, 100.0 + 0.15 * w, i, experience=50.0) for i, w in enumerate((8, 6, 4, 6, 8, 4))]
    known = optimise(_ds(clear), _obs({"VM_REAR_WING": 6}, 0, 99).setup,
                     {"pace": 1.0, "tyres": 0.0, "drivability": 0.0}, (50.0, 30.0), size="small")
    assert guess.confidence < 0.7 <= known.confidence


def test_driver_improvement_is_not_credited_to_the_setup():
    # Same setup three times, getting faster each time: that's the driver.
    obs = [_obs({}, 101.0 - 0.4 * i, i, experience=8.0 * i) for i in range(3)]
    ds = _ds(obs)
    res = optimise(ds, obs[-1].setup, W, (24.0, 30.0), size="small")
    assert res.experience_effect[0] < -0.3
    assert all(abs(e["delta"]["pace"][0]) < 0.05 for e in res.effects)


def test_trust_radius():
    r = 0.12
    assert next_radius(r, True) == pytest.approx(0.18)
    assert next_radius(r, False) == pytest.approx(0.084)
    assert next_radius(r, None) == r
    assert next_radius(RADIUS_MAX, True) == RADIUS_MAX and next_radius(RADIUS_MIN, False) == RADIUS_MIN


# -- end to end on the toy car -----------------------------------------------------------
def test_loop_beats_random_and_staying_on_the_toy_car():
    seeds, stints = range(4), 20
    large = np.mean([bench.run(stints, s, "model", "large").costs[-1] for s in seeds])
    small = np.mean([bench.run(stints, s, "model", "small").costs[-1] for s in seeds])
    random = np.mean([bench.run(stints, s, "random").costs[-1] for s in seeds])
    assert large < -0.05  # improved on the start (cost 0)
    assert large < small < random


def test_tc_controls_have_independent_groups():
    from autosetup.opt.optimizer import group_of
    from autosetup.opt.dataset import Dim
    d_map = Dim("VM_TRACTIONCONTROLMAP", ["VM_TRACTIONCONTROLMAP"], 0, 10)
    d_cut = Dim("VM_TRACTIONCONTROLPOWERCUTMAP", ["VM_TRACTIONCONTROLPOWERCUTMAP"], 0, 10)
    d_slip = Dim("VM_TRACTIONCONTROLSLIPANGLEMAP", ["VM_TRACTIONCONTROLSLIPANGLEMAP"], 0, 10)
    assert group_of(d_map) == "tc:map"
    assert group_of(d_cut) == "tc:cut"
    assert group_of(d_slip) == "tc:slip"


def test_alignment_and_controls_in_build_dims_and_groups():
    from autosetup.opt.dataset import OPT_BASES, build_dims, Dim
    from autosetup.lmu.setup_space import CarSetup
    from autosetup.opt.optimizer import group_of

    assert "WM_PRESSURE" not in OPT_BASES
    assert "VM_FRONT_TOEIN" in OPT_BASES
    assert "VM_REAR_TOEIN" in OPT_BASES
    assert "WM_CAMBER" in OPT_BASES
    assert "VM_CASTER" in OPT_BASES
    assert "VM_ENGINE_BRAKEMAP" in OPT_BASES
    assert "VM_BRAKE_PRESSURE" not in OPT_BASES  # the rules raise it until you flag lock-ups
    assert "VM_DIFF_PUMP" in OPT_BASES
    assert "VM_STEER_LOCK" in OPT_BASES

    data = {
        "VM_FRONT_TOEIN": dict(minValue=0, maxValue=20, value=5, stringValue="-0.10 deg", available=True),
        "VM_REAR_TOEIN": dict(minValue=0, maxValue=20, value=10, stringValue="+0.20 deg", available=True),
        "VM_LEFT_CASTER": dict(minValue=0, maxValue=15, value=8, stringValue="7.0 deg", available=True),
        "VM_RIGHT_CASTER": dict(minValue=0, maxValue=15, value=8, stringValue="7.0 deg", available=True),
        "WM_CAMBER-W_FL": dict(minValue=0, maxValue=30, value=20, stringValue="-3.0 deg", available=True),
        "WM_CAMBER-W_FR": dict(minValue=0, maxValue=30, value=20, stringValue="-3.0 deg", available=True),
        "WM_CAMBER-W_RL": dict(minValue=0, maxValue=30, value=15, stringValue="-2.0 deg", available=True),
        "WM_CAMBER-W_RR": dict(minValue=0, maxValue=30, value=15, stringValue="-2.0 deg", available=True),
        "WM_PRESSURE-W_FL": dict(minValue=0, maxValue=75, value=0, stringValue="136 kPa", available=True),
        "VM_ENGINE_BRAKEMAP": dict(minValue=0, maxValue=5, value=2, stringValue="2", available=True),
        "VM_BRAKE_PRESSURE": dict(minValue=0, maxValue=20, value=18, stringValue="95%", available=True),
        "VM_DIFF_PUMP": dict(minValue=0, maxValue=10, value=3, stringValue="30%", available=True),
        "VM_STEER_LOCK": dict(minValue=0, maxValue=10, value=5, stringValue="540 deg", available=True),
    }
    setup = CarSetup.from_json(data)
    dims = {d.name: d for d in build_dims(setup)}

    assert "VM_FRONT_TOEIN" in dims
    assert "VM_REAR_TOEIN" in dims
    assert "VM_CASTER" in dims
    assert dims["VM_CASTER"].keys == ["VM_LEFT_CASTER", "VM_RIGHT_CASTER"]
    assert "WM_CAMBER:F" in dims
    assert "WM_CAMBER:R" in dims
    assert "WM_PRESSURE:F" not in dims  # tyre pressure strictly excluded from optimizer
    assert "VM_ENGINE_BRAKEMAP" in dims
    assert "VM_BRAKE_PRESSURE" not in dims
    assert "VM_DIFF_PUMP" in dims
    assert "VM_STEER_LOCK" in dims

    assert group_of(dims["VM_FRONT_TOEIN"]) == "toe:F"
    assert group_of(dims["VM_REAR_TOEIN"]) == "toe:R"
    assert group_of(dims["WM_CAMBER:F"]) == "camber:F"
    assert group_of(dims["WM_CAMBER:R"]) == "camber:R"
    assert group_of(dims["VM_CASTER"]) == "caster"
    assert group_of(dims["VM_ENGINE_BRAKEMAP"]) == "engine_brake"
    assert group_of(dims["VM_DIFF_PUMP"]) == "diff"
    assert group_of(dims["VM_STEER_LOCK"]) == "steering"
