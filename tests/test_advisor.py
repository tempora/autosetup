import copy

import pytest

from autosetup.advisor import load_knowledge, propose
from autosetup.lmu.setup_space import CarSetup
from autosetup.scales import CarScales, Scale
from tests.conftest import setup_json

K = load_knowledge("GT3")


def _setup(extra: dict | None = None, **values) -> CarSetup:
    data = copy.deepcopy(setup_json())
    for k, v in (extra or {}).items():
        data[k] = {"available": True, **v}
    for k, (value, text) in values.items():
        data[k]["value"], data[k]["stringValue"] = value, text
    return CarSetup.from_json(data)


def _scales(**points) -> CarScales:
    return CarScales({k: Scale(k, dict(v)) for k, v in points.items()})


SCALES = _scales(**{
    "WM_CAMBER-W_FL": {26: "-2.4 deg", 28: "-2.2 deg"},
    "WM_CAMBER-W_FR": {26: "-2.4 deg", 28: "-2.2 deg"},
    "VM_BRAKE_BALANCE": {18: "52.5:47.5", 22: "51.5:48.5"},
    "VM_FRONT_ANTISWAY": {0: "Detached", 4: "P5", 5: "P6"},
    "VM_REAR_ANTISWAY": {3: "P4", 4: "P5"},
})
BASE = {"racing_laps": 5}
RAKE = {"WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"}  # as 'recent': rake was just tried, test the next tool


def run(summary=None, setup=None, size="small", **kw):
    return propose(1, {**BASE, **(summary or {})}, setup or _setup(), "GT3", SCALES, knowledge=K, size=size, **kw)


def test_needs_laps():
    pr = propose(1, {"racing_laps": 1}, _setup(), "GT3", SCALES, knowledge=K, size="small")
    assert not pr.changes and "2 full laps" in pr.notes[0]


def test_nothing_to_do_inside_windows():
    pr = run({"t_spread_fl": 8, "t_spread_fr": 9, "t_crown_fl": 5, "brake_temp_max_fl": 600})
    assert pr.changes == [] and pr.notes == []


def test_camber_moves_the_axle_together_in_the_right_direction():
    pr = run({"t_spread_fl": 15.0, "t_spread_fr": 13.0})  # 14 > 12 → less negative camber
    [c] = pr.changes
    assert c.keys == ["WM_CAMBER-W_FL", "WM_CAMBER-W_FR"] and c.name == "Camber FL/FR"
    assert c.after == {"WM_CAMBER-W_FL": 29, "WM_CAMBER-W_FR": 29}  # index up = less negative
    assert c.after_text == "-2.1 deg" and c.approx
    assert "less negative camber" in c.reason
    pr = run({"t_spread_fl": 1.0, "t_spread_fr": 1.0})  # 4 °C below the band → 2 steps more camber
    assert pr.changes[0].after["WM_CAMBER-W_FL"] == 26 and pr.changes[0].after_text == "-2.4 deg"


def test_pressure_at_minimum_stays_quiet():
    setup = _setup(extra={"WM_PRESSURE-W_FL": dict(minValue=0, maxValue=75, value=0, stringValue="136 kPa")})
    pr = run({"t_crown_fl": 12.0}, setup)  # wants lower pressure, can't go lower
    assert pr.changes == [] and pr.notes == []

def test_pressure_is_never_raised():
    setup = _setup(extra={"WM_PRESSURE-W_FL": dict(minValue=0, maxValue=75, value=10, stringValue="146 kPa")})
    assert run({"t_crown_fl": -2.0}, setup).changes == []  # cool centre: stays where it is
    [c] = run({"t_crown_fl": 12.0}, setup).changes  # hot centre: still comes down
    assert c.after["WM_PRESSURE-W_FL"] < 10 and "pressure down" in c.reason


def test_brake_ducts_open_when_hot():
    setup = _setup(extra={"VM_BRAKE_DUCTS": dict(minValue=0, maxValue=6, value=3, stringValue="60mm")})
    [c] = run({"brake_temp_max_fl": 1000.0, "brake_temp_max_fr": 900.0}, setup).changes
    assert c.after == {"VM_BRAKE_DUCTS": 2} and "1000 °C" in c.reason


def test_ride_height_raised_when_bottoming():
    setup = _setup(extra={k: dict(minValue=0, maxValue=30, value=5, stringValue="5.5 cm")
                          for k in ("WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR")})
    [c] = run({"bottoming_s": 0.5, "rh_min_fl": 0.5, "rh_min_fr": 1.0}, setup).changes
    assert c.after == {"WM_RIDEHEIGHT-W_FL": 6, "WM_RIDEHEIGHT-W_FR": 6} and "raise the front" in c.reason


def test_rated_mid_understeer_raises_the_rear_by_millimetres():
    # One 1 mm click of rake changes nothing; the move is ~2 mm a severity level.
    [c] = run(rating={"mid": 1}).changes
    assert c.keys == sorted(RAKE) and c.delta == 2 and "rake" in c.reason
    [c] = run(rating={"mid": 2}, size="large").changes
    assert c.delta == 8


def test_rated_mid_understeer_softens_front_bar():
    [c] = run(rating={"mid": 1}, recent=RAKE).changes
    assert c.keys == ["VM_FRONT_ANTISWAY"] and c.after["VM_FRONT_ANTISWAY"] == 4
    assert c.after_text == "P5" and not c.approx
    assert c.reason.startswith("You rated mid-corner understeer (+1)")


def test_lock_falls_through_to_next_option_and_says_so():
    pr = run(rating={"mid": 1}, locks={"VM_FRONT_ANTISWAY"}, recent=RAKE)
    [c] = pr.changes
    assert c.keys == ["VM_REAR_ANTISWAY"] and c.after["VM_REAR_ANTISWAY"] == 4
    assert "Front anti-roll bar is locked" in pr.notes


def test_never_detaches_a_bar():
    setup = _setup(VM_FRONT_ANTISWAY=(1, "P1 (soft)"))
    [c] = run(rating={"mid": 2}, setup=setup, recent=RAKE).changes  # softer front would hit 'Detached'
    assert c.keys == ["VM_REAR_ANTISWAY"]


def test_entry_understeer_moves_brake_bias_rearward():
    [c] = run(rating={"entry": 1}).changes
    assert c.keys == ["VM_BRAKE_BALANCE"]
    assert c.after["VM_BRAKE_BALANCE"] == 20  # 0.25 %/step → 2 steps ≈ 0.5 %
    assert c.after_text == "52.0:48.0"


def test_telemetry_oversteer_and_balance_cap():
    setup = _setup(extra={"VM_TRACTIONCONTROLMAP": dict(minValue=0, maxValue=12, value=5, stringValue="5")})
    pr = run({"cs_exit": 4.5, "cs_entry": 2.5, "cs_mid": 2.2}, setup)
    groups = [c for c in pr.changes if c.group == "balance"]
    assert len(groups) == 2  # capped
    assert groups[0].keys == ["VM_TRACTIONCONTROLMAP"]  # worst problem (severity 2) first
    assert "4.5 countersteer moments per lap" in groups[0].reason
    pr = run({"cs_exit": 4.5, "cs_entry": 2.5}, setup, weights={"drivability": 0.1})
    assert len([c for c in pr.changes if c.group == "balance"]) == 1


def test_countersteer_you_are_fine_with_is_not_oversteer():
    setup = _setup(extra={"VM_TRACTIONCONTROLMAP": dict(minValue=0, maxValue=12, value=5, stringValue="5")})
    assert run({"cs_exit": 3.0}, setup).changes  # no ratings yet: the usual threshold
    loose_ok = {"entry": 2.0, "mid": 2.0, "exit": 3.5}
    assert run({"cs_exit": 3.0}, setup, countersteer=loose_ok).changes == []
    [c] = run({"cs_exit": 7.5}, setup, countersteer=loose_ok).changes  # well past it: still a problem
    assert c.keys == ["VM_TRACTIONCONTROLMAP"] and "fine up to about 3.0" in c.reason


def _proto(kgf: int, abs_adjustable: bool = False) -> CarSetup:
    """LMU's brake pressure: index 0..80 = 40..120 kgf, 120 kgf = 100 %."""
    return _setup(extra={
        "VM_BRAKE_PRESSURE": dict(minValue=0, maxValue=81, value=kgf - 40, stringValue=f"{kgf} kgf ({round(kgf / 1.2)}%)"),
        "VM_ANTILOCKBRAKESYSTEMMAP": dict(minValue=0, maxValue=10 if abs_adjustable else 1, value=0, stringValue="N/A"),
    })


def _pressure(car_class: str, kgf: int, abs_adjustable: bool = False, **kw):
    pr = propose(1, dict(BASE), _proto(kgf, abs_adjustable), car_class, SCALES, knowledge=load_knowledge(car_class),
                 size="small", **kw)
    return [(c.after["VM_BRAKE_PRESSURE"] + 40, c.reason) for c in pr.changes if c.keys == ["VM_BRAKE_PRESSURE"]]


def test_brake_pressure_rises_inside_the_comfortable_range_until_you_say_too_much():
    # LMP2: 90–94 %, i.e. 108–112 kgf.
    [(kgf, why)] = _pressure("LMP2_ELMS", 105)
    assert kgf == 108 and "below the usual 90–94 %" in why  # straight to the bottom of the range
    [(kgf, why)] = _pressure("LMP2_ELMS", 108)
    assert kgf == 110 and "wasn't too much" in why  # then a step per stint ...
    assert _pressure("LMP2_ELMS", 112) == []  # ... up to the top of it
    [(kgf, _)] = _pressure("LMP2_ELMS", 118)
    assert kgf == 112  # above it: down to the top
    [(kgf, why)] = _pressure("LMP2_ELMS", 110, rating={"lockups": True})
    assert kgf == 108 and "too much brake pressure" in why
    # Too much at 110 kgf before: never back above 109.
    [(kgf, _)] = _pressure("LMP2_ELMS", 108, brake_ceiling=109 - 40)
    assert kgf == 109
    [(kgf, why)] = _pressure("LMP2_ELMS", 111, brake_ceiling=109 - 40)
    assert kgf == 109 and "where you said it was too much (92 %)" in why


def test_brake_pressure_on_hypercars_and_cars_with_abs():
    assert _pressure("Hyper", 120) == []  # 100 % is inside 93–100 %
    [(kgf, _)] = _pressure("Hyper", 112)
    assert kgf == 114
    assert _pressure("GT3", 100, abs_adjustable=True) == []  # ABS: left alone
    assert _pressure("LMP2_ELMS", 100, abs_adjustable=True) == []


TC = {"VM_TRACTIONCONTROLMAP": dict(minValue=0, maxValue=12, value=7, stringValue="7")}


def test_busy_traction_control_alone_is_not_oversteer():
    # More TC cuts in more: TC time must not ask for more TC (that only ratchets up).
    [c] = run({"tc_s": 20.0}, _setup(extra=TC)).changes
    assert c.keys == ["VM_TRACTIONCONTROLMAP"] and c.delta == -1 and "less traction control" in c.reason
    [c] = run({"tc_s": 20.0, "cs_exit": 4.5}, _setup(extra=TC)).changes
    assert c.keys == ["VM_TRACTIONCONTROLMAP"] and c.delta > 0  # countersteer: that's oversteer
    assert run({"tc_s": 20.0}, _setup(extra=TC), rating={"exit": -1}).changes[0].delta > 0
    assert run({"tc_s": 3.0}, _setup(extra=TC)).changes == []


def test_entry_understeer_raises_the_rear_after_brake_bias():
    [c] = run(rating={"entry": 1}, recent={"VM_BRAKE_BALANCE"}).changes
    assert c.keys == sorted(RAKE) and c.delta == 2 and "rake" in c.reason


def test_signature_tracks_the_values():
    a, b = run(rating={"mid": 1}), run(rating={"mid": 1})
    assert a.signature == b.signature
    assert run(rating={"mid": 2}).signature != a.signature


AERO = {"VM_REAR_WING": dict(minValue=0, maxValue=10, value=6, stringValue="12.3 deg"),
        "WM_RIDEHEIGHT-W_FL": dict(minValue=0, maxValue=30, value=2, stringValue="5.2 cm"),
        "WM_RIDEHEIGHT-W_FR": dict(minValue=0, maxValue=30, value=2, stringValue="5.2 cm")}


def test_persisting_understeer_rotates_to_another_tool():
    setup = _setup(extra=AERO)
    first = run(rating={"mid": 1}, setup=setup, recent=RAKE).changes
    assert [c.keys for c in first] == [["VM_FRONT_ANTISWAY"]]
    again = run(rating={"mid": 1}, setup=setup, recent=RAKE | {"VM_FRONT_ANTISWAY"}).changes
    assert [c.keys for c in again] == [["VM_REAR_ANTISWAY"]]
    third = run(rating={"mid": 1}, setup=setup, recent=RAKE | {"VM_FRONT_ANTISWAY", "VM_REAR_ANTISWAY"}).changes
    assert third[0].keys == ["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"] and third[0].delta == -1


def test_exit_understeer_can_take_rear_wing_off():
    setup = _setup(extra=AERO)
    [c] = run(rating={"exit": 1}, setup=setup, recent=RAKE | {"VM_FRONT_ANTISWAY"}).changes
    assert c.keys == ["VM_REAR_WING"] and c.delta == -1 and "less rear wing" in c.reason


def test_a_car_pointier_than_your_feel_gets_rotation_taken_out():
    from autosetup.opt.balance import learn

    # Mid-speed mid corner at 1.0° with the usual 1.65° target: two points pointier, not negative.
    assert run({"us_medium_mid": 1.0}).changes == []  # no feel picked, no ratings: the default isn't yours
    lazier = learn([], K).with_feel(front=1).targets  # Lazier: the target is yours, and higher
    [c] = run({"us_medium_mid": 1.0}, targets=lazier).changes
    assert c.source == "telemetry" and "more rotation than you like" in c.reason
    assert c.keys == ["VM_REAR_ANTISWAY"] and c.delta < 0  # mid oversteer: softer rear bar first
    pointier = learn([], K).with_feel(front=-1).targets  # ... while Pointier is happy with 1.3°
    assert run({"us_medium_mid": 1.3}, targets=pointier).changes == []


def test_zone_ratings_use_that_speeds_tools_for_that_phase():
    setup = _setup(extra=AERO)
    # "Oversteer on low-speed corner exits": slow corners are mechanical, so the rear bar first.
    [c] = run(rating={"slow_exit": -1}, setup=setup).changes
    assert c.keys == ["VM_REAR_ANTISWAY"] and c.delta < 0 and "low-speed corner exits oversteer" in c.reason
    # "Understeer on mid-speed corner entries": the entry tools first (brake bias).
    [c] = run(rating={"medium_entry": 1}, setup=setup).changes
    assert c.keys == ["VM_BRAKE_BALANCE"] and "mid-speed corner entries understeer (+1)" in c.reason
    # High-speed oversteer mid corner: aero first.
    [c] = run(rating={"fast_mid": -2}, setup=setup).changes
    assert c.keys == ["VM_REAR_WING"] and c.delta > 0
    # "A little" only teaches what you like; nothing is changed for it directly.
    assert run(rating={"fast_mid": 0.5}, setup=setup).changes == []
    # Ratings from before the zones still work (a whole corner speed).
    [c] = run(rating={"fast": -1}, setup=setup).changes
    assert c.keys == ["VM_REAR_WING"] and "high-speed corners oversteer" in c.reason


def test_wing_is_the_last_resort_where_it_costs_lap_time():
    setup = _setup(extra=AERO)
    recent = RAKE | {"VM_FRONT_ANTISWAY"}
    [c] = run(rating={"exit": 1}, setup=setup, recent=recent).changes
    assert c.keys == ["VM_REAR_WING"]  # a neutral track: less wing is next in line
    [c] = run(rating={"exit": 1}, setup=setup, recent=recent, wing_effect=-0.4).changes
    assert c.keys != ["VM_REAR_WING"]  # a downforce track: less wing costs time, so it comes last
    [c] = run(rating={"exit": 1}, setup=setup, recent=recent, wing_effect=0.4).changes
    assert c.keys == ["VM_REAR_WING"]  # a drag track: less wing is fine


def test_understeer_from_telemetry_above_the_usual_band():
    [c] = run({"us_medium_mid": 2.4}).changes
    assert c.source == "telemetry" and "Mid-speed mid-corner: understeer angle 2.40" in c.reason
    assert run({"us_medium_mid": 1.9}).changes == []


def test_large_size_moves_further():
    setup = _setup(extra=AERO)
    small = run(rating={"entry": 2, "mid": 2, "exit": 2}, setup=setup, size="small").changes
    large = run(rating={"entry": 2, "mid": 2, "exit": 2}, setup=setup, size="large").changes
    assert len(large) >= len(small)
    bb_small = next(c for c in small if c.keys == ["VM_BRAKE_BALANCE"])
    bb_large = next(c for c in large if c.keys == ["VM_BRAKE_BALANCE"])
    assert abs(bb_large.delta) == 2 * abs(bb_small.delta)


def test_front_lowered_despite_light_kerb_contact():
    setup = _setup(extra=AERO)
    [c] = run({"rh_min_fl": 27.0, "rh_min_fr": 28.0, "bottoming_s": 0.05}, setup=setup).changes
    assert c.group == "platform" and c.delta == -1
    assert run({"rh_min_fl": 27.0, "rh_min_fr": 28.0, "bottoming_s": 0.5}, setup=setup).changes[0].delta == 1


def test_learned_rotation_sets_when_telemetry_complains():
    from autosetup.opt.balance import learn

    pointy = learn([("slow_entry", 0.08, 0), ("slow_entry", 0.12, 0), ("slow_entry", 0.50, 1)], K).targets
    assert run({"us_slow_entry": 0.6}).changes == []  # the GT3 default lets 0.6° pass ...
    [c] = run({"us_slow_entry": 0.6}, targets=pointy).changes  # ... you don't
    assert c.source == "telemetry" and "you like about" in c.reason and c.keys == ["VM_FRONT_ANTISWAY"]
    [c] = run({"us_slow_entry": -0.4}, targets=pointy).changes  # rated twice or more: too pointy counts too
    assert "more rotation than you like" in c.reason
    once = learn([("slow_entry", 0.10, 0)], K).targets
    assert run({"us_slow_entry": -0.4}, targets=once).changes == []


DAMPERS = {f"WM_{d}-W_{w}": dict(minValue=0, maxValue=41, value=20, stringValue=str(20))
           for d in ("SLOWBUMP", "SLOWREBOUND") for w in ("FL", "FR", "RL", "RR")}


def test_dampers_are_balance_tools():
    [c] = run(rating={"entry": 1}, setup=_setup(extra=DAMPERS), recent=RAKE | {"VM_BRAKE_BALANCE"}).changes
    assert c.keys == ["WM_SLOWREBOUND-W_RL", "WM_SLOWREBOUND-W_RR"] and c.delta > 0
    [c] = run(rating={"exit": -1}, setup=_setup(extra=DAMPERS)).changes
    assert c.keys == ["WM_SLOWBUMP-W_RL", "WM_SLOWBUMP-W_RR"] and c.delta < 0


def test_platform_does_not_ratchet_the_rear_up():
    # A balanced car that isn't bottoming keeps its rake: raising the rear every stint
    # would add rotation whether or not you wanted it.
    rear = {k: dict(minValue=0, maxValue=30, value=5, stringValue="6.0 cm") for k in ("WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR")}
    setup = _setup(extra={**AERO, **rear})
    pr = run({"rh_min_fl": 20.0, "rh_min_fr": 20.0, "rh_min_rl": 30.0, "rh_min_rr": 30.0, "bottoming_s": 0.05},
             setup=setup, weights={"pace": 1.0})
    assert pr.changes == []


def test_busy_tc_rotates_to_slip_and_cut():
    all_tc = {
        "VM_TRACTIONCONTROLMAP": dict(minValue=0, maxValue=12, value=7, stringValue="7"),
        "VM_TRACTIONCONTROLSLIPANGLEMAP": dict(minValue=0, maxValue=12, value=5, stringValue="5"),
        "VM_TRACTIONCONTROLPOWERCUTMAP": dict(minValue=0, maxValue=12, value=6, stringValue="6"),
    }
    setup = _setup(extra=all_tc)
    [c1] = run({"tc_s": 20.0}, setup, recent={"VM_TRACTIONCONTROLMAP"}).changes
    assert c1.keys == ["VM_TRACTIONCONTROLSLIPANGLEMAP"] and c1.delta == -1
    assert "less restrictive TC slip" in c1.reason

    [c2] = run({"tc_s": 20.0}, setup, recent={"VM_TRACTIONCONTROLMAP", "VM_TRACTIONCONTROLSLIPANGLEMAP"}).changes
    assert c2.keys == ["VM_TRACTIONCONTROLPOWERCUTMAP"] and c2.delta == -1
    assert "less TC power cut" in c2.reason


def test_exit_oversteer_rotates_through_tc_controls():
    all_tc = {
        "VM_TRACTIONCONTROLMAP": dict(minValue=0, maxValue=12, value=7, stringValue="7"),
        "VM_TRACTIONCONTROLSLIPANGLEMAP": dict(minValue=0, maxValue=12, value=5, stringValue="5"),
        "VM_TRACTIONCONTROLPOWERCUTMAP": dict(minValue=0, maxValue=12, value=6, stringValue="6"),
    }
    setup = _setup(extra=all_tc)
    [c1] = run({"cs_exit": 4.5}, setup, recent={"VM_TRACTIONCONTROLMAP"}).changes
    assert c1.keys == ["VM_TRACTIONCONTROLSLIPANGLEMAP"] and c1.delta == 1
    assert "stricter TC slip angle" in c1.reason
