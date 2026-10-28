import numpy as np
import pytest

from autosetup.advisor import load_knowledge
from autosetup.opt import bench
from autosetup.opt.dataset import Dataset, build_dims
from autosetup.opt.drag import wing_effect
from autosetup.opt.optimizer import optimise

K = load_knowledge("GT3")
W = {"pace": 1.0, "tyres": 0.5, "drivability": 0.75}


def test_wing_effect_from_the_share_of_the_lap_flat_out():
    # Until 3 layouts, against the knowledge file's usual share (GT3 0.60).
    assert wing_effect({"Spa": 0.70}, "Spa", K) == pytest.approx(0.4)
    assert wing_effect({"Lusail": 0.55}, "Lusail", K) == pytest.approx(-0.2)
    assert wing_effect({}, "Spa", K) == 0.0  # no complete lap yet
    assert wing_effect({"Le Mans": 0.95}, "Le Mans", K) == pytest.approx(K["wing_prior_max_s"])  # capped
    # From 3 layouts, against your own median.
    shares = {"A": 0.50, "B": 0.55, "C": 0.70}
    assert wing_effect(shares, "C", K) == pytest.approx(4.0 * 0.15)
    assert wing_effect(shares, "B", K) == pytest.approx(0.0)


def test_drag_prior_makes_more_wing_cost_lap_time():
    rng = np.random.default_rng(0)
    setup = bench.make_setup({**bench.start_values(), "VM_REAR_WING": 4})  # room both ways
    obs = [bench.drive_stint(setup, 8.0 * i, rng, i) for i in range(3)]
    ds = Dataset("toy", build_dims(setup), obs, 0.0, 0.0, 0.25)

    def wing_step(prior):
        res = optimise(ds, setup, W, (24.0, 30.0), pace_prior=prior)
        return next(e for e in res.effects if e["dim"] == "VM_REAR_WING")["delta"]["pace"][0]

    without = wing_step(None)
    with_drag = wing_step({"": {"VM_REAR_WING": 0.6}})  # "toy" has no layout: its stints' group is ""
    steps = build_dims(setup)[[d.name for d in build_dims(setup)].index("VM_REAR_WING")].hi
    assert with_drag - without == pytest.approx(0.6 / steps, abs=0.02)
