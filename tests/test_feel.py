"""Sliders, target, lock and transfer, on recorded Interlagos stints (current build)."""

import pytest

from autosetup.feel import fingerprint as fpm
from autosetup.feel.cars import car_key, model_of
from autosetup.feel.measure import CELLS, measured
from autosetup.feel.target import RANGE_STEPS, axis_weight, set_slider, sliders, target
from autosetup.knowledge import load_knowledge
from autosetup.store import Store

K = load_knowledge("GT3")
STEP = K["feel_step_deg"]
SPAN = RANGE_STEPS * STEP
NONE = {"front": 0.0, "rear": 0.0, "fast": 0.0}


def slide(store, key, rid, axis, value):
    set_slider(store, key, "GT3", rid, axis, value, SPAN)


@pytest.fixture
def store(recorded, tmp_path):
    st = Store(tmp_path / "db.duckdb")
    ids = {n: st.import_recording(recorded(n)) for n in ("interlagos_arb_r3", "interlagos_arb_r5", "interlagos_wing_11")}
    st.ids = ids
    return st


def test_car_model_from_the_saved_setup_class():
    assert model_of("GT3 Porsche_911_GT3_R_LMGT3 WEC2025", "GT3") == "Porsche_911_GT3_R_LMGT3"
    assert model_of("ELMS2026 LMP2_ELMS Oreca_07", "LMP2_ELMS") == "Oreca_07"
    assert model_of("Hypercar Peugeot_9x8 WEC2025", "Hyper") == "Peugeot_9x8"
    assert model_of("BMW_M4_LMGT3 GT3 WEC2025", "GT3") == "BMW_M4_LMGT3"


def test_car_key_falls_back_to_the_livery_until_matched(store):
    assert car_key(store, "Iron Dames 2025 #85", "GT3") == "Iron Dames 2025 #85"
    store.con.execute("insert into car_classes values ('Iron Dames 2025 #85', 'GT3 Porsche_911_GT3_R_LMGT3 WEC2025')")
    assert car_key(store, "Iron Dames 2025 #85", "GT3") == "Porsche_911_GT3_R_LMGT3"


def test_no_sliders_no_lock_aims_at_what_you_drive(store):
    rid = store.ids["interlagos_arb_r5"]
    t = target(store, "car", "GT3", K, rid)
    m = measured(store, [rid])
    assert t.anchor == "current" and t.anchor_stint == rid
    assert t.cells == pytest.approx(m.mean())
    assert all(t.tolerance[c] >= K["feel_tolerance_deg"] for c in t.cells)


def test_sliders_move_the_target_from_their_stint(store):
    first, later = store.ids["interlagos_arb_r3"], store.ids["interlagos_arb_r5"]
    base = measured(store, [first]).mean()
    slide(store, "car", first, "front", -STEP)
    slide(store, "car", first, "rear", 0.6 * STEP)
    slide(store, "car", first, "front", -STEP)  # a position, not a count
    assert sliders(store, first) == {"front": -STEP, "rear": 0.6 * STEP, "fast": 0.0}
    t = target(store, "car", "GT3", K, first)
    assert t.anchor == "moved" and t.anchor_stint == first and t.span == pytest.approx(SPAN)
    for c in t.cells:
        move = -STEP * axis_weight("front", c) + 0.6 * STEP * axis_weight("rear", c)
        assert t.cells[c] == pytest.approx(base[c] + move)
        if abs(move) > 2 * t.deadzone["front"] + 0.2:  # well past what the stint resolves: you drove outside the band
            assert t.tolerance[c] < abs(move)
    # A new stint starts as it was, from its own balance.
    assert sliders(store, later) == NONE
    t = target(store, "car", "GT3", K, later)
    assert t.anchor == "current" and t.cells == pytest.approx(measured(store, [later]).mean())
    slide(store, "car", first, "front", 0)
    assert sliders(store, first)["front"] == 0.0
    slide(store, "car", first, "rear", 5.0)  # past the end: the end
    assert sliders(store, first)["rear"] == pytest.approx(SPAN)
    with pytest.raises(ValueError):
        slide(store, "car", first, "faster", 0.1)


def test_a_move_inside_the_dead_zone_changes_nothing(store):
    rid = store.ids["interlagos_arb_r3"]
    still = target(store, "car", "GT3", K, rid)
    dz = still.deadzone["front"]
    assert dz > 0
    slide(store, "car", rid, "front", -0.5 * dz)
    t = target(store, "car", "GT3", K, rid)
    for c in t.cells:  # the band still holds what you drove
        assert abs(t.cells[c] - still.cells[c]) <= t.tolerance[c] + 1e-9


def test_fast_slider_moves_only_fast_corners_and_makes_them_count(store):
    rid = store.ids["interlagos_arb_r3"]
    base = measured(store, [rid]).mean()
    slide(store, "car", rid, "fast", STEP)
    t = target(store, "car", "GT3", K, rid, layout_mix={"low": 0.95, "high": 0.05})
    for c in t.cells:
        if c.endswith("_low") or c.startswith("braking_"):
            assert t.cells[c] == pytest.approx(base[c])
    assert t.cells["mid_high"] == pytest.approx(base["mid_high"] + STEP)
    assert t.weights["mid_high"] >= 0.5 and t.weights["mid_low"] == 0.95


def test_lock_freezes_the_balance_and_becomes_the_anchor(store):
    rid = store.ids["interlagos_arb_r5"]
    fp_key = fpm.recording_car(store, rid)[0]
    slide(store, fp_key, rid, "rear", -STEP)
    fp = fpm.lock(store, rid, K)
    assert fp.car_key == fpm.recording_car(store, rid)[0]
    m = measured(store, [rid])
    assert fp.cells == pytest.approx(m.mean())
    assert set(fp.platform) >= {"wing", "rh_front", "rh_rear", "rake", "rh_front_fast", "rake_fast", "aero_balance"}
    assert "arb_rear" in fp.setup and fp.setup["arb_rear"]["text"] == "P5"
    t = target(store, fp.car_key, "GT3", K, store.ids["interlagos_wing_11"])
    assert t.anchor == "fingerprint" and t.fingerprint == fp.id and t.cells == pytest.approx(fp.cells)
    assert sliders(store, rid) == NONE  # the stint was it, as it was
    # A slider on a later stint goes from that stint, not the fingerprint.
    later = store.ids["interlagos_wing_11"]
    slide(store, fp.car_key, later, "rear", -STEP)
    t = target(store, fp.car_key, "GT3", K, later)
    m = measured(store, [later])
    assert t.anchor == "moved" and t.cells["exit_high"] == pytest.approx(m.mean()["exit_high"] - STEP)
    assert t.countersteer == pytest.approx(max(0.0, m.countersteer + 0.5))
    fp2 = fpm.lock(store, store.ids["interlagos_arb_r3"], K)
    assert fpm.own(store, fp.car_key).id == fp2.id  # the newest lock is the car's


def test_fingerprint_carries_to_another_track_and_the_class(store):
    rid = store.ids["interlagos_arb_r5"]
    fp = fpm.lock(store, rid, K)
    # Same car, a layout it hasn't been driven at: the fingerprint is the target.
    t = target(store, fp.car_key, "GT3", K, None, layout_mix={"low": 0.2, "high": 0.8})
    assert t.anchor == "fingerprint" and t.cells == pytest.approx(fp.cells)
    assert t.weights["entry_low"] == 0.2 and t.weights["exit_high"] == 0.8
    # Another GT3 without a fingerprint of its own: the class's, with a wider band.
    other = target(store, "BMW_M4_LMGT3", "GT3", K, None)
    assert other.anchor == "fingerprint" and other.cells == pytest.approx(fp.cells)
    assert all(other.tolerance[c] >= 1.5 * fp.tolerance[c] - 1e-9 for c in fp.cells)
    # Another class: nothing to go on.
    assert target(store, "Oreca_07", "LMP2_ELMS", K, None).cells == {}


def test_profile_axes_and_summary(store):
    fp = fpm.lock(store, store.ids["interlagos_arb_r5"], K)
    base = {c: fp.cells[c] + 0.5 for c in fp.cells}  # the class understeers more than this car
    rear, front = fpm.axes(fp, base, STEP)
    assert rear == pytest.approx(-2.0) and front == pytest.approx(-2.0)
    line = fpm.summary(fp, base, STEP, usual_rake=(fp.platform["rake_fast"] or 0) - 6)
    assert line.startswith("Very pointy entry, very loose exit") and "more rake than usual" in line
    assert fpm.summary(fp, dict(fp.cells), STEP, None) == "Neutral entry, neutral exit"
    assert set(fpm.usual(store, "GT3")) <= set(CELLS)
