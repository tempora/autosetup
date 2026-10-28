"""The rules: tyres, brake cooling, brake pressure, platform safety (rules.py)."""

import copy

from autosetup.knowledge import load_knowledge
from autosetup.lmu.setup_space import CarSetup
from autosetup.rules import propose
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
AERO = {"VM_REAR_WING": dict(minValue=0, maxValue=10, value=6, stringValue="12.3 deg"),
        "WM_RIDEHEIGHT-W_FL": dict(minValue=0, maxValue=30, value=2, stringValue="5.2 cm"),
        "WM_RIDEHEIGHT-W_FR": dict(minValue=0, maxValue=30, value=2, stringValue="5.2 cm")}


def run(summary=None, setup=None, **kw):
    return propose(1, {**BASE, **(summary or {})}, setup or _setup(), "GT3", SCALES, knowledge=K, **kw)


def _proto(kgf: int, abs_adjustable: bool = False) -> CarSetup:
    """LMU's brake pressure: index 0..80 = 40..120 kgf, 120 kgf = 100 %."""
    return _setup(extra={
        "VM_BRAKE_PRESSURE": dict(minValue=0, maxValue=81, value=kgf - 40, stringValue=f"{kgf} kgf ({round(kgf / 1.2)}%)"),
        "VM_ANTILOCKBRAKESYSTEMMAP": dict(minValue=0, maxValue=10 if abs_adjustable else 1, value=0, stringValue="N/A"),
    })


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


def _pressure(car_class: str, kgf: int, abs_adjustable: bool = False, **kw):
    pr = propose(1, dict(BASE), _proto(kgf, abs_adjustable), car_class, SCALES, knowledge=load_knowledge(car_class),
                 **kw)
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


def test_signature_tracks_the_values():
    a, b = run({"t_spread_fl": 15.0, "t_spread_fr": 13.0}), run({"t_spread_fl": 15.0, "t_spread_fr": 13.0})
    assert a.changes and a.signature == b.signature
    assert run({"t_spread_fl": 1.0, "t_spread_fr": 1.0}).signature != a.signature


def test_lowering_is_left_to_the_optimiser():
    """A car with room to spare isn't lowered by a rule (the optimiser knows what the
    downforce is worth); a bottoming one is still raised."""
    setup = _setup(extra=AERO)
    assert run({"rh_min_fl": 27.0, "rh_min_fr": 28.0, "bottoming_s": 0.05}, setup=setup).changes == []
    assert run({"rh_min_fl": 27.0, "rh_min_fr": 28.0, "bottoming_s": 0.5}, setup=setup).changes[0].delta == 1


def test_balance_is_not_a_rule_any_more():
    """Understeer, countersteer and busy traction control are the optimiser's, against your target."""
    tc = {"VM_TRACTIONCONTROLMAP": dict(minValue=0, maxValue=12, value=7, stringValue="7")}
    assert run({"us_mid": 3.0, "cs_exit": 5.0, "tc_s": 20.0}, _setup(extra=tc)).changes == []
