"""The damper block: contact patch inside the band, kerbs no worse, whole clicks."""

import numpy as np
import pytest

from autosetup.knowledge import load_knowledge
from autosetup.opt import dampers
from autosetup.opt.space import Dim

DIMS = [Dim("slow_bump_front", ("SBF",), 0, 20), Dim("fast_bump_front", ("FBF",), 0, 20)]
BASE = {"slow_bump_front": 10, "fast_bump_front": 10}


def _data(seed=0, n=6):
    """Stints where stiffer front slow bump raises front load variation and stiffer fast
    bump raises the kerb load spikes."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        x = {"slow_bump_front": rng.uniform(0.2, 0.8), "fast_bump_front": rng.uniform(0.2, 0.8)}
        out.append(dampers.StintMetrics(i, x, {
            "cv_front": (0.20 + 0.06 * x["slow_bump_front"] + rng.normal(0, 0.002), 0.003 ** 2),
            "cv_rear": (0.18 + rng.normal(0, 0.002), 0.003 ** 2),
            "peak_load": (1.30 + 0.5 * x["fast_bump_front"] - 0.3 * x["slow_bump_front"] + rng.normal(0, 0.01), 0.01 ** 2),
        }))
    return out


def test_softens_slow_bump_for_the_contact_patch_without_kerb_cost():
    model = dampers.DamperModel.fit(DIMS, _data())
    choice = dampers.choose(model, DIMS, BASE, {}, lambda idx: 1.0)
    assert choice.moves and not choice.exploring
    assert choice.moves["slow_bump_front"][1] < 10  # softer: steadier front load
    assert abs(choice.moves["slow_bump_front"][1] - 10) <= dampers.MAX_CLICKS
    assert choice.contact[0] < choice.contact_base[0]
    # Softening slow bump raises the kerb spikes in this data; the move stays within the margin.
    x = {d.name: d.x(choice.moves.get(d.name, (BASE[d.name], BASE[d.name]))[1]) for d in DIMS}
    assert model.kerb_ok(x, {d.name: d.x(BASE[d.name]) for d in DIMS}) >= 0.5


def test_kerbs_veto_a_move_that_makes_them_worse():
    model = dampers.DamperModel.fit(DIMS, _data())
    base_x = {d.name: d.x(BASE[d.name]) for d in DIMS}
    stiffer_fast = {**base_x, "fast_bump_front": DIMS[1].x(20)}
    assert model.kerb_ok(stiffer_fast, base_x) < 0.5


def test_the_balance_band_vetoes_damper_moves():
    model = dampers.DamperModel.fit(DIMS, _data())
    assert dampers.choose(model, DIMS, BASE, {}, lambda idx: 0.0).moves == {}


def test_one_stint_only_explores_and_says_so():
    """One stint can't show what a click does: a move then is to learn, and says so."""
    model = dampers.DamperModel.fit(DIMS, _data(n=1))
    choice = dampers.choose(model, DIMS, BASE, {}, lambda idx: 1.0)
    assert choice.moves and choice.exploring and "learning" in choice.reason
    assert all(abs(b - a) <= dampers.MAX_CLICKS for a, b in choice.moves.values())


def test_priors_say_what_each_damper_is_for():
    fast = dampers._prior("fast_bump_front")
    slow = dampers._prior("slow_bump_rear")
    assert fast["peak_load"][1] > fast["cv_front"][1]  # fast bump: kerbs first
    assert slow["cv_rear"][1] > slow["peak_load"][1]  # slow bump: contact patch first
    assert "cv_front" not in slow
    packers = dampers._prior("third_packers_rear")
    assert packers["heave_mm"][0] < 0


def test_metrics_and_histograms_of_a_recorded_stint(recorded, tmp_path):
    from autosetup.store import Store

    store = Store(tmp_path / "db.duckdb")
    rid = store.import_recording(recorded("interlagos_wing_11"))
    k = load_knowledge("GT3")
    m = dampers.stint_metrics(store, rid, k)
    assert {"cv_front", "cv_rear", "sym_front", "knee_front", "shape", "peak_load", "airtime_s"} <= set(m)
    assert all(np.isfinite(v[0]) and v[1] > 0 for v in m.values())
    h = dampers.histograms(store, rid, None, 50.0, k)
    both = sum(h["wheels"]["fl"]["counts"])
    off = sum(dampers.histograms(store, rid, False, 50.0, k)["wheels"]["fl"]["counts"])
    on = sum(dampers.histograms(store, rid, True, 50.0, k)["wheels"]["fl"]["counts"])
    assert both == on + off and on > 0
    edges = h["wheels"]["fl"]["edges"]
    assert edges[1] - edges[0] == pytest.approx(50.0) and edges[0] < 0 < edges[-1]


def test_many_stints_on_one_setting_still_explore():
    """Ten stints all on the same dampers teach nothing about a click: still a learning move."""
    same = [dampers.StintMetrics(i, {"slow_bump_front": 0.5, "fast_bump_front": 0.5}, s.values)
            for i, s in enumerate(_data(n=10))]
    model = dampers.DamperModel.fit(DIMS, same)
    assert model.stints == 10 and model.settings == 1
    choice = dampers.choose(model, DIMS, BASE, {}, lambda idx: 1.0)
    assert choice.moves and choice.exploring


def test_enough_settings_with_no_effect_give_no_move():
    """Four different settings and nothing changes with them: no move, not even to learn."""
    rng = np.random.default_rng(1)
    flat = [dampers.StintMetrics(i, {"slow_bump_front": x, "fast_bump_front": 1 - x},
                                 {"cv_front": (0.2 + rng.normal(0, 0.001), 0.002 ** 2),
                                  "cv_rear": (0.18 + rng.normal(0, 0.001), 0.002 ** 2)})
            for i, x in enumerate((0.2, 0.4, 0.6, 0.8))]
    model = dampers.DamperModel.fit(DIMS, flat)
    assert model.settings == 4
    assert dampers.choose(model, DIMS, BASE, {}, lambda idx: 1.0).moves == {}
