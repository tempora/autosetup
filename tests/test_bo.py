"""The constrained search and the transfer of a fingerprint (toy car, opt/bench.py)."""

import numpy as np

from autosetup.feel.measure import CELLS
from autosetup.feel.target import shifted
from autosetup.opt import bench, bo
from autosetup.opt.balance_model import BalanceModel, Evidence
from autosetup.opt.platform import Platform
from autosetup.opt.priors import sensitivities

DIMS = bench.DIMS
MID = {d.name: (d.lo + d.hi) // 2 for d in DIMS}
SLOPES = sensitivities(DIMS, bench.KNOWLEDGE)


def _model(cells_at_mid: dict[str, float]) -> BalanceModel:
    ev = Evidence("L", bo.x_of(DIMS, MID), {c: (v, 0.05) for c, v in cells_at_mid.items()}, True)
    return BalanceModel.fit(SLOPES, "L", [ev])


def _band(cells, tol=0.15):
    return bo.TargetBand(dict(cells), {c: tol for c in cells}, {c: 0.5 for c in cells}, 0.25)


FLAT = {c: 0.6 for c in CELLS}
NO_LAP = lambda idx: (0.0, 5.0)  # noqa: E731


def test_nothing_asked_nothing_to_gain_stays():
    choice = bo.solve(bo.Problem(DIMS, MID, NO_LAP, _model(FLAT), _band(FLAT)))
    assert choice.reason == "stay" and choice.moves == {} and choice.feasible


def test_a_pointier_target_gets_entry_rotation():
    """One press: a setup in the new band, entries rotating more, exits where they were.
    Two presses ask for more than the trust region reaches without moving the exits: the
    closest setup, said to be the closest."""
    one = shifted(FLAT, front=-1, rear=0, step=0.25)
    choice = bo.solve(bo.Problem(DIMS, MID, NO_LAP, _model(FLAT), _band(one)))
    assert choice.feasible and choice.reason == "balance" and choice.moves
    pred = choice.best.balance
    assert pred["entry_low"][0] < FLAT["entry_low"] - 0.1
    assert abs(pred["exit_low"][0] - FLAT["exit_low"]) <= 0.15
    two = shifted(FLAT, front=-2, rear=0, step=0.25)
    choice = bo.solve(bo.Problem(DIMS, MID, NO_LAP, _model(FLAT), _band(two)))
    assert choice.moves and choice.best.off < bo.violation(choice.base, _band(two))


def test_wing_first_then_balance_around_it():
    """The lap is fastest at the most wing; the wing goes there and the understeer it adds
    in fast corners is put back with the other settings."""
    def lap(idx):
        return 100.0 - 0.3 * idx["wing"], 0.02
    choice = bo.solve(bo.Problem(DIMS, MID, lap, _model(FLAT), _band(FLAT), radius=0.5))
    assert choice.best.indices["wing"] == 8 and choice.wing["index"] == 8
    assert choice.feasible and len(choice.moves) >= 2  # wing plus compensation
    assert bo.violation(choice.best, _band(FLAT)) < 0.05


def test_the_band_wins_and_the_gap_says_what_it_costs():
    """Rotation on exit is lap time here, whatever gives it, but the band holds the exits:
    the setup stays in the band and the gap reports what more rotation would be worth."""
    model = _model(FLAT)

    def lap(idx):
        pred = model.predict(bo.x_of(DIMS, idx))
        return 100.0 + 0.8 * sum(pred[c][0] for c in ("mid_low", "exit_low", "mid_high", "exit_high")), 0.02
    choice = bo.solve(bo.Problem(DIMS, MID, lap, model, _band(FLAT, tol=0.1)))
    assert choice.feasible and bo.violation(choice.best, _band(FLAT, tol=0.1)) < 0.05
    gap = [g for g in choice.gap if g["axis"] == "rear"]
    assert gap and gap[0]["seconds"] >= 0.1 and gap[0]["steps"] in (1, 2)


def test_a_bottoming_car_is_not_lowered():
    def lap(idx):  # lower is faster
        return 100.0 + 0.05 * idx["rh_front"] + 0.05 * idx["rh_rear"], 0.01
    plat = Platform({"front": 3.0, "rear": 10.0}, bottoming_s=0.5, min_mm=2.0, bottoming_limit_s=0.2,
                    mm_per_click={"rh_front": 1.0, "rh_rear": 1.0})
    choice = bo.solve(bo.Problem(DIMS, MID, lap, _model(FLAT), _band(FLAT, tol=1.0), plat))
    assert choice.best.indices["rh_front"] >= MID["rh_front"] and choice.best.indices["rh_rear"] >= MID["rh_rear"]


def test_an_unreachable_target_gets_the_closest_setup():
    far = {c: v - 5.0 for c, v in FLAT.items()}
    choice = bo.solve(bo.Problem(DIMS, MID, NO_LAP, _model(FLAT), _band(far)))
    assert not choice.feasible and choice.moves
    assert np.mean([choice.best.balance[c][0] for c in CELLS]) < np.mean(list(FLAT.values()))


def test_the_loop_reaches_the_band_and_gets_faster():
    for seed in (0, 1):
        w = bench.world(seed)
        target = {c: v - 0.3 for c, v in w.balance(MID).items()}
        hist = bench.run(w, target, MID, runs=6, seed=seed)
        assert hist[0]["off"] > 0.5
        assert hist[-1]["off"] < 0.2 and min(h["off"] for h in hist[1:]) < 0.05
        assert hist[0]["lap"] - hist[-1]["lap"] > 0.2


def test_a_fingerprint_carries_to_a_new_track():
    """Five stints at track A make the evidence and the fingerprint; track B puts the car
    0.15° elsewhere. The first proposal at B, before any stint there, starts from A's
    evidence and the fingerprint and lands close; without them it can't."""
    a = bench.world(3)
    target = {c: v - 0.3 for c, v in a.balance(MID).items()}
    hist = bench.run(a, target, MID, runs=5, seed=3, layout="A")
    rng = np.random.default_rng(0)
    evidence = [Evidence("A", bo.x_of(DIMS, h["idx"]),
                         {c: (v + rng.normal(0, 0.05), 0.08) for c, v in a.balance(h["idx"]).items()}, False)
                for h in hist]
    fp_idx = hist[-1]["idx"]
    b = bench.World(a.slopes, {c: v + 0.15 for c, v in a.level.items()}, 4)
    model = BalanceModel.fit(SLOPES, "B", evidence, (bo.x_of(DIMS, fp_idx), target))
    assert model.source == "other layouts"
    band = _band(target)
    first = bo.solve(bo.Problem(DIMS, MID, NO_LAP, model, band, candidates=[fp_idx])).best.indices
    off = sum(max(0.0, abs(b.balance(first)[c] - target[c]) - 0.15) for c in CELLS)
    off_start = sum(max(0.0, abs(b.balance(MID)[c] - target[c]) - 0.15) for c in CELLS)
    assert off < 0.5 * off_start and off < 0.4
    # And refined after one run there (B's own stint in the evidence).
    hist_b = bench.run(b, target, first, runs=3, seed=5, evidence=evidence, layout="B")
    assert hist_b[-1]["off"] < 0.25


def test_toe_and_tender_springs_are_levers_where_the_car_has_them():
    from autosetup.knowledge import load_knowledge
    from autosetup.lmu.setup_space import CarSetup
    from autosetup.opt.priors import sensitivities
    from autosetup.opt.space import main_dims

    def p(lo, hi, v, text):
        return {"available": True, "minValue": lo, "maxValue": hi, "value": v, "stringValue": text}
    data = {"VM_FRONT_TOEIN": p(0, 17, 7, "-0.117 deg"), "VM_REAR_TOEIN": p(0, 17, 10, "0.234 deg"),
            **{f"WM_TENDERSPRING-W_{w}": p(0, 2, 1, "Standard") for w in ("FL", "FR")},
            **{f"WM_TENDERSPRING-W_{w}": p(0, 1, 0, "Standard") for w in ("RL", "RR")}}
    dims = main_dims(CarSetup.from_json(data))
    assert [d.name for d in dims] == ["toe_front", "toe_rear", "tender_front"]  # the rear tender is fixed
    s = sensitivities(dims, load_knowledge("GT3"))
    assert s["toe_front"]["entry_low"] > 0  # toe-in at the front: lazier entry
    assert s["toe_rear"]["exit_low"] > 0  # toe-in at the rear: safer exit
    assert s["tender_front"]["mid_low"] < 0  # tender on: a softer front, more rotation


# -- toe: rarely, a click at a time, never front toe-in ------------------------------------

def _toe_car(front: int, rear: int, step: float = 0.0625):
    """A car whose toe runs −0.5° … +0.5° over 17 clicks (or the other way, step < 0)."""
    from autosetup.lmu.setup_space import CarSetup
    from autosetup.scales import CarScales, Scale
    from autosetup.opt.space import main_dims

    def deg(i):
        return (-0.5 + 0.0625 * i) if step > 0 else (0.5 - 0.0625 * i)

    def p(v):
        return {"available": True, "minValue": 0, "maxValue": 17, "value": v, "stringValue": f"{deg(v):.4f} deg"}
    setup = CarSetup.from_json({"VM_FRONT_TOEIN": p(front), "VM_REAR_TOEIN": p(rear)})
    scales = CarScales({k: Scale(k, {0: f"{deg(0):.4f} deg", 16: f"{deg(16):.4f} deg"})
                        for k in ("VM_FRONT_TOEIN", "VM_REAR_TOEIN")})
    return setup, scales, main_dims(setup)


def test_front_toe_never_goes_positive():
    from autosetup.proposals import _front_toe_limit
    setup, scales, dims = _toe_car(front=7, rear=9)  # front −0.0625°
    front = next(d for d in _front_toe_limit(dims, setup, scales) if d.name == "toe_front")
    assert (front.lo, front.hi) == (0, 8)  # index 8 is 0°
    assert front.x(4) == next(d for d in dims if d.name == "toe_front").x(4)  # same scale as unlimited
    # Already on toe-in (+0.125° at 10): it can only come back.
    setup, scales, dims = _toe_car(front=10, rear=9)
    assert next(d for d in _front_toe_limit(dims, setup, scales) if d.name == "toe_front").hi == 10
    # Labels that run the other way: the limit is at the bottom of the range.
    setup, scales, dims = _toe_car(front=9, rear=9, step=-0.0625)  # front −0.0625°
    front = next(d for d in _front_toe_limit(dims, setup, scales) if d.name == "toe_front")
    assert (front.lo, front.hi) == (8, 16)


def test_a_fingerprint_candidate_cannot_take_front_toe_positive():
    from autosetup.proposals import _front_toe_limit
    setup, scales, dims = _toe_car(front=7, rear=9)
    dims = _front_toe_limit(dims, setup, scales)
    base = {d.name: d.index(setup) for d in dims}
    model = BalanceModel({}, {})
    choice = bo.solve(bo.Problem(dims, base, lambda idx: (100.0 + 0.1 * idx["toe_front"], 0.01), model, None,
                                 candidates=[{"toe_front": 15}], reach={"toe_front": 1, "toe_rear": 1}))
    assert choice.best.indices["toe_front"] <= 8


def test_rear_toe_out_only_for_a_clear_gain():
    from autosetup.proposals import REAR_TOE_OUT_S, TOE_COST_S, _rear_toe_out
    setup, scales, dims = _toe_car(front=7, rear=8)  # rear at 0°
    rear = next(d for d in dims if d.name == "toe_rear")
    penalty = _rear_toe_out(rear, setup, scales)
    assert penalty({"toe_rear": 7}) == REAR_TOE_OUT_S and penalty({"toe_rear": 8}) == 0.0
    base = {d.name: d.index(setup) for d in dims}
    toe = {"toe_front": TOE_COST_S, "toe_rear": TOE_COST_S}

    def solve(gain):
        lap = lambda idx: (100.0 - gain * (idx["toe_rear"] < 8), 0.01)  # noqa: E731
        return bo.solve(bo.Problem(dims, base, lap, BalanceModel({}, {}), None, reach={"toe_front": 1, "toe_rear": 1},
                                   cost=toe, penalty=penalty)).best.indices["toe_rear"]
    assert solve(0.4) == 7
    assert solve(0.15) == 8  # gains less than toe-out costs
    # A car already on toe-out isn't charged for staying there.
    setup, scales, dims = _toe_car(front=7, rear=6)
    assert _rear_toe_out(next(d for d in dims if d.name == "toe_rear"), setup, scales) is None


def test_toe_moves_a_click_and_a_bar_wins_a_tie():
    """A bar click and a toe click do the same to the balance: the bar is chosen, and toe
    never moves more than its reach."""
    from autosetup.opt.space import Dim
    from autosetup.proposals import TOE_COST_S
    dims = [Dim("arb_front", ("AF",), 0, 16), Dim("toe_front", ("TF",), 0, 16)]
    base = {"arb_front": 8, "toe_front": 8}
    slopes = {n: {c: 1.6 for c in CELLS} for n in base}  # 0.1° a click, both
    model = BalanceModel.fit(slopes, "L", [Evidence("L", bo.x_of(dims, base), {c: (0.6, 0.02) for c in CELLS}, True)])
    target = _band({c: 0.5 for c in CELLS}, tol=0.03)  # a click of either
    choice = bo.solve(bo.Problem(dims, base, NO_LAP, model, target, reach={"toe_front": 1},
                                 cost={"toe_front": TOE_COST_S}))
    assert choice.moves == {"arb_front": (8, 7)}
    far = _band({c: 0.2 for c in CELLS}, tol=0.03)  # needs four clicks in all
    choice = bo.solve(bo.Problem(dims, base, NO_LAP, model, far, reach={"toe_front": 1},
                                 cost={"toe_front": TOE_COST_S}))
    assert abs(choice.best.indices["toe_front"] - 8) <= 1


def test_moves_of_an_undrivable_stint_are_not_repeated():
    """The short stint took the rear bar 3 clicks stiffer: no candidate goes 3 or more that way."""
    model = _model(FLAT)
    choice = bo.solve(bo.Problem(DIMS, MID, NO_LAP, model, _band(shifted(FLAT, 0, -2, 0.25)),
                                 avoid={"arb_rear": (1, 3)}, radius=0.5))
    assert choice.best.indices["arb_rear"] - MID["arb_rear"] < 3
    assert not bo.avoided(bo.Problem(DIMS, MID, NO_LAP, model, None, avoid={"arb_rear": (1, 3)}),
                          {**MID, "arb_rear": MID["arb_rear"] - 1})


def test_effect_per_click_does_not_shrink_with_more_clicks():
    """A 19-click bar and an 11-click bar: the same effect per click (sizes per real step)."""
    from autosetup.knowledge import load_knowledge
    from autosetup.opt.space import Dim
    k = load_knowledge("GT3")
    long, short = Dim("arb_front", ("A",), 0, 18), Dim("arb_front", ("A",), 0, 10)
    a, b = sensitivities([long], k)["arb_front"], sensitivities([short], k)["arb_front"]
    assert abs(a["mid_low"] / 18 - b["mid_low"] / 10) < 1e-9
    assert abs(a["mid_low"] / 18 - k["step_deg_arb"]) < 1e-9  # its strongest cell, a click


def test_ride_height_and_toe_steps_follow_their_labels():
    """Per mm of ride height and per 0.1° of toe, read from the labels; without readable
    labels, the old whole-range size."""
    from autosetup.knowledge import load_knowledge
    from autosetup.opt.space import Dim
    from autosetup.scales import CarScales, Scale
    k = load_knowledge("GT3")
    rh = Dim("rh_rear", ("WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"), 0, 40)
    toe = Dim("toe_rear", ("VM_REAR_TOEIN",), 0, 16)
    scales = CarScales({"WM_RIDEHEIGHT-W_RL": Scale("WM_RIDEHEIGHT-W_RL", {0: "5.0 cm", 40: "9.0 cm"}),
                        "VM_REAR_TOEIN": Scale("VM_REAR_TOEIN", {0: "-0.5 deg", 16: "0.5 deg"})})
    s = sensitivities([rh, toe], k, scales)
    assert abs(abs(s["rh_rear"]["mid_high"]) / 40 - k["step_deg_ride_height"]) < 1e-9  # a click is 1 mm
    per_click = 0.0625 / 0.1 * k["step_deg_toe"]
    assert abs(max(abs(v) for v in s["toe_rear"].values()) / 16 - per_click) < 1e-9
    bare = sensitivities([Dim("toe_rear", ("T",), 0, 16)], k, CarScales({}))
    assert abs(max(abs(v) for v in bare["toe_rear"].values()) - 0.7 * k["prior_full_range_deg"]) < 1e-9


def test_rake_before_a_bar_and_a_bar_a_click_at_a_time():
    """A ride-height move and a bar click do the same to the balance: the ride height is
    chosen. Asking for more never takes the bar more than a click."""
    from autosetup.opt.space import Dim
    from autosetup.proposals import CHANGE_COST, REACH, _by_kind
    dims = [Dim("rh_rear", ("R",), 0, 16), Dim("arb_rear", ("AR",), 0, 16)]
    base = {"rh_rear": 8, "arb_rear": 8}
    slopes = {n: {c: -1.6 for c in CELLS} for n in base}  # 0.1° more rotation a click, both
    model = BalanceModel.fit(slopes, "L", [Evidence("L", bo.x_of(dims, base), {c: (0.6, 0.02) for c in CELLS}, True)])
    cost = {d.name: _by_kind(CHANGE_COST, d.name) for d in dims}
    reach = {"arb_rear": _by_kind(REACH, "arb_rear"), "rh_rear": 5}
    choice = bo.solve(bo.Problem(dims, base, NO_LAP, model, _band({c: 0.5 for c in CELLS}, tol=0.03),
                                 cost=cost, reach=reach))
    assert choice.moves == {"rh_rear": (8, 9)}
    choice = bo.solve(bo.Problem(dims, base, NO_LAP, model, _band({c: -0.2 for c in CELLS}, tol=0.03),
                                 cost=cost, reach=reach))
    assert abs(choice.best.indices["arb_rear"] - 8) <= 1


def test_diff_ramps_are_levers_where_the_car_has_them():
    """LMP2 ramps ('FF6-30 deg', lower locks more) and a Hypercar front diff (%, higher locks
    more); a locked front diff isn't searched."""
    from autosetup.knowledge import load_knowledge
    from autosetup.lmu.setup_space import CarSetup
    from autosetup.opt.priors import index_direction
    from autosetup.opt.space import main_dims
    from autosetup.scales import CarScales, Scale, _value_of

    def p(lo, hi, v, text, ok=True):
        return {"available": ok, "minValue": lo, "maxValue": hi, "value": v, "stringValue": text}
    assert _value_of("VM_DIFF_COAST", "FF6-30 deg") == 30
    lmp2 = CarSetup.from_json({"VM_DIFF_POWER": p(0, 6, 0, "FF6-60 deg"), "VM_DIFF_COAST": p(0, 6, 3, "FF6-30 deg")})
    dims = {d.name: d for d in main_dims(lmp2)}
    assert {"diff_power", "diff_coast"} <= set(dims)
    ramps = CarScales({"VM_DIFF_COAST": Scale("VM_DIFF_COAST", {1: "FF6-55 deg", 3: "FF6-30 deg"})})
    assert index_direction("locking", dims["diff_coast"], ramps) == 1  # angle falls with the index
    s = sensitivities([dims["diff_coast"]], load_knowledge("LMP2"), ramps)
    assert s["diff_coast"]["entry_low"] > 0  # more locking up the index: lazier turn-in
    hyper = CarSetup.from_json({"VM_FRONT_DIFF_POWER": p(0, 10, 2, "20%"), "VM_FRONT_DIFF_COAST": p(0, 10, 0, "10%"),
                                "VM_FRONT_DIFF_PRELOAD": p(0, 10, 0, "0 Nm", ok=False)})
    assert {d.name for d in main_dims(hyper)} == {"front_diff_power", "front_diff_coast"}


def test_the_front_wing_is_a_lever_and_the_aero_wing_on_cars_without_a_rear_one():
    from autosetup.knowledge import load_knowledge
    from autosetup.lmu.setup_space import CarSetup
    from autosetup.opt.space import Dim, aero_wing, main_dims
    from autosetup.sim.model import wing_share

    def p(lo, hi, v, text, ok=True):
        return {"available": ok, "minValue": lo, "maxValue": hi, "value": v, "stringValue": text}
    both = CarSetup.from_json({"VM_REAR_WING": p(0, 10, 4, "P5"), "VM_FRONT_WING": p(0, 10, 2, "P3")})
    dims = main_dims(both)
    assert [d.name for d in dims] == ["wing", "front_wing"] and aero_wing(dims).name == "wing"
    s = sensitivities(dims, load_knowledge("Hyper"))
    assert s["front_wing"]["mid_high"] < 0  # more front wing: less understeer in fast corners
    front_only = CarSetup.from_json({"VM_REAR_WING": p(0, 10, 4, "P5", ok=False), "VM_FRONT_WING": p(0, 10, 2, "P3")})
    dims = main_dims(front_only)
    assert aero_wing(dims).name == "front_wing"
    assert wing_share(front_only) == front_only["VM_FRONT_WING"].fraction
    # The drag level is set on the front wing first.
    fw = [Dim("front_wing", ("VM_FRONT_WING",), 0, 8), Dim("rh_rear", ("R",), 0, 30)]
    choice = bo.solve(bo.Problem(fw, {"front_wing": 2, "rh_rear": 15}, lambda idx: (100.0 + 0.3 * idx["front_wing"], 0.02),
                                 BalanceModel({}, {}), None, radius=0.5))
    assert choice.wing["index"] < 2 and choice.best.indices["front_wing"] < 2
