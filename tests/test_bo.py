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
