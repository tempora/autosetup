import copy

import pytest

from autosetup.lmu.setup_space import CarSetup, parse_number
from tests.conftest import setup_json


def test_ranges_are_min_inclusive_max_exclusive():
    s = CarSetup.from_json(setup_json())
    rh = s["WM_RIDEHEIGHT-W_RL"]
    assert (rh.min, rh.max, rh.steps) == (2, 51, 49)
    assert rh.clamp(0) == 2 and rh.clamp(99) == 50


def test_adjustable_excludes_single_step_settings():
    keys = {p.key for p in CarSetup.from_json(setup_json()).adjustable()}
    assert "VM_GEAR_1" not in keys
    assert {"VM_REAR_ANTISWAY", "WM_CAMBER-W_FL"} <= keys


def test_corner_and_base():
    p = CarSetup.from_json(setup_json())["WM_CAMBER-W_FL"]
    assert p.corner == "FL" and p.base == "WM_CAMBER"
    assert CarSetup.from_json(setup_json())["VM_BRAKE_BALANCE"].corner is None


def test_signature_ignores_values_but_not_ranges():
    a = setup_json()
    b = copy.deepcopy(a)
    b["VM_REAR_ANTISWAY"]["value"] = 1
    c = copy.deepcopy(a)
    c["VM_REAR_ANTISWAY"]["maxValue"] = 9
    sig = CarSetup.from_json(a).signature
    assert CarSetup.from_json(b).signature == sig
    assert CarSetup.from_json(c).signature != sig


def test_non_dict_entries_are_ignored():
    s = CarSetup.from_json(setup_json())
    assert "gearGraph" not in s and "symmetric" not in s
    assert s.symmetric is True


def test_steering_ratio():
    assert CarSetup.from_json(setup_json()).steering_ratio == 16.5


@pytest.mark.parametrize("text,expected", [
    ("-2.2 deg", -2.2), ("52.5:47.5", 52.5), ("8,500", 8500.0), ("1 (soft)", 1.0),
    ("120 kgf (100%)", 120.0), ("Open", None), ("D35x1_1-1", None), ("N/A", None), (" 0.0", 0.0),
])
def test_parse_number(text, expected):
    assert parse_number(text) == expected
