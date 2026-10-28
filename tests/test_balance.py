import collections

import numpy as np
import pytest

from autosetup.advisor import load_knowledge, understeer_signs
from autosetup.opt import bench
from autosetup.opt.balance import Linear, learn, zone_ratings
from autosetup.opt.dataset import ZONES, Dataset, Observation, build_dims
from autosetup.opt.optimizer import optimise
from autosetup.scales import CarScales

K = load_knowledge("GT3")
W = {"pace": 1.0, "tyres": 0.5, "drivability": 0.75}
M = "medium_mid"  # the zone these tests steer: mid corner in mid-speed corners


# -- what you like ---------------------------------------------------------------------
def test_without_ratings_targets_sit_a_point_below_the_usual_limits():
    b = learn([], K)
    for z in ZONES:
        t = b.targets[z]
        assert t.n == 0 and t.points(K[f"understeer_{z}"]) == pytest.approx(1.0)


def test_ratings_learn_a_pointier_car_and_it_carries_over():
    # Low-speed entries rated neutral around 0.1° and understeery at 0.5°: far more rotation
    # than the GT3 default (0.35° liked, 0.77° is where it complains).
    rated = [("slow_entry", 0.08, 0), ("slow_entry", 0.12, 0), ("slow_entry", 0.50, 1), ("slow_entry", 0.54, 1)]
    b, usual = learn(rated, K), learn([], K)
    assert b.targets["slow_entry"].angle < 0.2 and b.targets["slow_entry"].n == 4
    assert b.shift < 0  # in general: more rotation than usual ...
    # ... most of all on entries at other speeds, a little elsewhere.
    pts = {z: (usual.targets[z].angle - b.targets[z].angle) / b.targets[z].per_point for z in ("fast_entry", "fast_mid")}
    assert pts["fast_entry"] > pts["fast_mid"] > 0


def test_ratings_from_before_the_zones_count_for_every_zone_they_cover():
    z = zone_ratings({"exit": -1, "fast": 1, "slow_exit": -2, "mid": None})
    assert z["slow_exit"] == -2  # a zone you rated itself wins
    assert z["medium_exit"] == -1 and z["fast_exit"] == -1  # the whole phase
    assert z["fast_entry"] == 1 and z["fast_mid"] == 1  # the whole corner speed
    assert "slow_entry" not in z


def test_car_feel_front_and_rear():
    b = learn([], K)
    assert b.with_feel() is b
    pointier = b.with_feel(front=-1)
    for z in ("slow_entry", "medium_mid", "fast_mid"):  # Pointier: a point more rotation on entry and mid, every speed
        t = pointier.targets[z]
        assert t.angle == pytest.approx(b.targets[z].angle - K[f"rating_point_{z}"]) and t.chosen
    assert pointier.targets["slow_exit"] == b.targets["slow_exit"]  # the rear is its own choice
    safer = b.with_feel(rear=1)
    assert safer.targets["fast_exit"].angle == pytest.approx(b.targets["fast_exit"].angle + K["rating_point_fast_exit"])
    assert safer.targets[M] == b.targets[M]
    # Looser also accepts a countersteer moment per lap more, Safer one less (not under 1).
    base = b.tolerance(K)["exit"]
    assert b.with_feel(rear=-1).tolerance(K)["exit"] == pytest.approx(base + 1.0)
    assert safer.tolerance(K)["exit"] == pytest.approx(base - 1.0)
    assert not b.targets[M].chosen  # the learned ones stay as they were


def test_balance_inside_the_range_you_like_costs_nothing():
    from autosetup.opt.optimizer import Model

    bal = learn([(M, 1.2, 0), (M, 1.2, 0)], K)
    target = bal.targets[M]
    costs = []
    for off_points in (0.0, 0.4, 1.5):  # inside the band, at its edge, well outside
        angle = target.angle + off_points * target.per_point
        obs = [_obs({"VM_FRONT_ANTISWAY": fa}, angle, i) for i, fa in enumerate((8, 8, 8))]
        ds = Dataset("toy", build_dims(obs[-1].setup), obs, 0.0, 0.0, 0.25)
        with_bal = Model(ds, bal, understeer_signs(CarScales({})))
        without = Model(ds, None, None)
        X = ds.X([obs[-1].setup], [(50.0, 30.0)])
        eps = np.zeros((1, 3 + len(ZONES)))
        costs.append(with_bal.z(X, eps, sample=False)[0, 2] - without.z(X, eps, sample=False)[0, 2])
    assert costs[0] == pytest.approx(0.0, abs=1e-6) and costs[1] == pytest.approx(0.0, abs=1e-6)
    assert costs[2] > 0.03


def test_countersteer_tolerance_is_learned_from_ratings():
    def tol(rows, layout=None):
        return learn([], K, rows).tolerance(K, layout)

    assert tol([]) == {ph: K["countersteer_per_lap"] for ph in ("entry", "mid", "exit")}
    # Exit countersteered 3/lap and you didn't call it oversteer: that much is fine.
    assert tol([("exit", 3.0, 0, "Spa")]) == {"entry": 2.0, "mid": 2.0, "exit": 3.5}
    # ... until you rate it oversteer at 2.5/lap; and a later stint can accept more again.
    assert tol([("exit", 3.0, 0, "Spa"), ("exit", 2.5, -1, "Spa")])["exit"] == 2.5
    assert tol([("exit", 3.0, 0, "Spa"), ("exit", 2.5, -1, "Spa"), ("exit", 4.0, 1, "Spa")])["exit"] == 4.5
    # Oversteer rated with little countersteer is about the angle, not countersteer.
    assert tol([("exit", 3.0, 0, "Spa"), ("exit", 1.0, -1, "Spa")])["exit"] == 3.5
    # A naturally loose track: its own ratings have the final word there.
    rows = [("exit", 4.0, 0, "Spa"), ("exit", 2.5, -1, "Monza")]
    assert tol(rows, "Spa")["exit"] == 4.5 and tol(rows, "Monza")["exit"] == 2.5


# -- what the setup does to it -------------------------------------------------------------
def test_linear_model_learns_a_slope_and_can_overrule_its_prior():
    rng = np.random.default_rng(0)
    X = np.column_stack([rng.uniform(0, 1, 6), rng.uniform(0, 1, 6)])
    y = 1.5 - 0.6 * X[:, 0] + rng.normal(0, 0.02, 6)  # the first setting rotates the car
    noise = np.full(6, 0.02 ** 2)
    for prior in (+0.6, -0.6):  # right and wrong direction
        lin = Linear(X, y, noise, None, np.array([prior, 0.0]), np.array([0.45, 0.3]), 0.05)
        assert lin.slopes()[0] < -0.4
        assert abs(lin.slopes()[1]) < 0.15


def _obs(values, angle_mid, i):
    setup = bench.make_setup({**bench.start_values(), **values})
    y = {"pace": 100.0, "tyres": 0.5, "drivability": 0.2}
    noise = {"pace": 0.25 ** 2 / 8, "tyres": 1e-4, "drivability": 1e-3}
    return Observation(i, f"s{i}", setup, y, noise, 50.0, 30.0, 8,
                       balance={M: (angle_mid, 0.03 ** 2)})


def test_model_moves_the_car_towards_the_rotation_you_like():
    # Mid-corner understeer follows the front bar; you like ~1.3°, the car runs ~2.1°.
    obs = [_obs({"VM_FRONT_ANTISWAY": fa}, 0.9 + 0.15 * fa, i) for i, fa in enumerate((8, 6, 7, 8, 5, 8))]
    ds = Dataset("toy", build_dims(obs[-1].setup), obs, 0.0, 0.0, 0.25)
    bal = learn([(M, 2.0, 2), (M, 1.2, 0)], K)
    signs = understeer_signs(CarScales({}))
    res = optimise(ds, obs[-1].setup, W, (50.0, 30.0), size="medium", balance=bal, understeer=signs)
    # Rotation comes back (by the bar, or springs/wing whose direction is known physics) ...
    assert res.balance[M][0] < -0.3 and res.confidence > 0.7
    assert all(m.steps < 0 for m in res.moves if m.dim.name == "VM_FRONT_ANTISWAY")
    # ... and the bar's effect is its known direction at a fixed size, not learned from these
    # stints (truth here +0.15° a click): what a track's stints say doesn't carry to the next.
    from autosetup.opt.balance import PRIOR_POINTS

    effect = next(e for e in res.effects if e["dim"] == "VM_FRONT_ANTISWAY")
    bar = next(d for d in ds.dims if d.name == "VM_FRONT_ANTISWAY")
    fixed = PRIOR_POINTS * K[f"rating_point_{M}"] / (bar.hi - bar.lo)
    assert effect["balance"][M][0] == pytest.approx(fixed, rel=0.02)


def test_model_raises_the_rear_for_an_understeering_car():
    # Mid-corner understeer follows rake only; the rear has been run at a few heights.
    from autosetup.lmu.setup_space import CarSetup

    def stint(rear, i):
        data = {f"WM_RIDEHEIGHT-W_{w}": dict(minValue=0, maxValue=30, value=v, stringValue=str(v), available=True)
                for w, v in (("FL", 5), ("FR", 5), ("RL", rear), ("RR", rear))}
        return Observation(i, f"s{i}", CarSetup.from_json(data), {"pace": 100.0, "tyres": 0.5, "drivability": 0.2},
                           {"pace": 0.25 ** 2 / 8, "tyres": 1e-4, "drivability": 1e-3}, 50.0, 30.0, 8,
                           balance={M: (3.0 - 0.1 * rear, 0.03 ** 2)})

    obs = [stint(r, i) for i, r in enumerate((10, 8, 9, 10, 7, 10))]
    ds = Dataset("toy", build_dims(obs[-1].setup), obs, 0.0, 0.0, 0.25)
    assert [d.name for d in ds.dims] == ["WM_RIDEHEIGHT:R"]  # the front stays with the rules
    bal = learn([(M, 2.0, 2), (M, 1.2, 0)], K)
    res = optimise(ds, obs[-1].setup, W, (50.0, 30.0), size="medium", balance=bal,
                   understeer=understeer_signs(CarScales({})))
    [m] = res.moves
    assert m.steps > 0 and res.balance[M][0] < -0.1


# -- exploring --------------------------------------------------------------------------------
def test_learning_tries_the_dampers_not_just_the_springs():
    # A setting with few positions used to look most informative every time (one click
    # is a big share of its range), so springs flipped every stint and dampers never moved.
    picked = collections.Counter()
    for seed in range(6):
        rng = np.random.default_rng(seed)
        setup = bench.make_setup(bench.start_values())
        obs = []
        for i in range(4):
            obs.append(bench.drive_stint(setup, 8.0 * i, rng, i))
            ds = Dataset("toy", build_dims(setup), obs, 0.0, 0.0, 0.25)
            res = optimise(ds, setup, W, (8.0 * (i + 1), 30.0), size="large", learning=True, seed=seed * 10 + i)
            picked.update(m.dim.name.split(":")[0] for m in res.moves)
            setup = bench._apply(setup, {k: m.after for m in res.moves for k in m.dim.keys})
    total = sum(picked.values())
    dampers = sum(v for k, v in picked.items() if k in ("WM_SLOWBUMP", "WM_SLOWREBOUND", "WM_FASTBUMP", "WM_FASTREBOUND"))
    assert dampers / total > 0.05
    assert picked["WM_SPRING"] / total < 0.2
    assert picked["VM_FRONT_ANTISWAY"] + picked["VM_REAR_ANTISWAY"] > 0


def test_model_takes_rotation_out_when_the_car_is_pointier_than_you_like():
    # The mirror of the above: you like 1.2° mid, the car runs ~0.4°. More rotation than
    # you like costs drivability too, or the model would push an oversteering car further.
    obs = [_obs({"VM_FRONT_ANTISWAY": fa}, -0.8 + 0.15 * fa, i) for i, fa in enumerate((8, 6, 7, 8, 5, 8))]
    ds = Dataset("toy", build_dims(obs[-1].setup), obs, 0.0, 0.0, 0.25)
    bal = learn([(M, 0.4, -2), (M, 1.2, 0)], K)
    signs = understeer_signs(CarScales({}))
    res = optimise(ds, obs[-1].setup, W, (50.0, 30.0), size="medium", balance=bal, understeer=signs)
    assert res.balance[M][0] > 0.2 and res.confidence > 0.7
