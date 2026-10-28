"""Third (heave) spring: packer metrics, packer rules, and the model's third-spring settings."""

from types import SimpleNamespace

import numpy as np

from autosetup.metrics.compute import _third_contacts, _third_spring
from autosetup.metrics.signals import GRID_HZ
from autosetup.opt.dataset import build_dims
from autosetup.opt.optimizer import group_of
from tests.test_advisor import _setup, run

THIRD = {
    "VM_FRONT_3RD_PACKERS": dict(minValue=0, maxValue=21, value=14, stringValue="1.4 cm"),
    "VM_REAR_3RD_PACKERS": dict(minValue=0, maxValue=11, value=5, stringValue="0.5 cm"),
    "VM_FRONT_3RD_SPRING": dict(minValue=0, maxValue=5, value=1, stringValue="1000 lbf/in"),
    "VM_REAR_3RD_SLOWBUMP": dict(minValue=0, maxValue=12, value=3, stringValue="4"),
    "WM_RIDEHEIGHT-W_FL": dict(minValue=0, maxValue=40, value=20, stringValue="3.7 cm"),
    "WM_RIDEHEIGHT-W_FR": dict(minValue=0, maxValue=40, value=20, stringValue="3.7 cm"),
}


def _signals(front_mm, rear_mm, kmh):
    return SimpleNamespace(third=np.column_stack([front_mm, rear_mm]) / 1000.0, speed=kmh / 3.6)


def test_packer_contact_is_the_knee_against_speed_squared():
    kmh = 50.0 + 115.0 * (1 - np.cos(np.linspace(0, 12 * np.pi, 120 * GRID_HZ)))  # 50–280 km/h
    load = (kmh / 100.0) ** 2
    free = 2.0 + 1.5 * load
    front = np.where(free < 7.0, free, 7.0 + 0.05 * (free - 7.0))  # packers from ~7 mm
    rear = 1.0 + 1.2 * load  # never reaches them
    sig, setup = _signals(front, rear, kmh), _setup(THIRD)
    contact = _third_contacts(sig, setup)
    assert abs(contact["f"] - 7.0) < 0.4 and contact["r"] is None
    m = _third_spring(sig, contact)
    assert abs(m["third_packers_f"] - np.mean(free >= 7.0)) < 0.08
    assert m["third_packers_r"] == 0.0 and "third_contact_r" not in m
    assert m["third_travel_f"] < 7.5


def test_no_third_spring_metrics_without_a_third_spring():
    kmh = np.linspace(50, 280, 60 * GRID_HZ)
    sig = _signals(40 + kmh / 10, 60 + kmh / 10, kmh)  # GT3s log the channel with unrelated values
    assert _third_contacts(sig, _setup()) == {}
    assert _third_spring(sig, {}) == {}


def test_bottoming_without_packer_contact_adds_packer_not_ride_height():
    setup = _setup(THIRD)
    pr = run({"bottoming_s": 1.0, "rh_min_fl": 0.5, "rh_min_fr": 0.6, "third_packers_f": 0.01}, setup)
    [c] = [c for c in pr.changes if c.group == "platform"]
    assert c.keys == ["VM_FRONT_3RD_PACKERS"] and c.delta == 1
    assert "on the packers only 1%" in c.reason
    # Already on the packers a lot: the packers aren't the fix, so the ride height goes up.
    pr = run({"bottoming_s": 1.0, "rh_min_fl": 0.5, "rh_min_fr": 0.6, "third_packers_f": 0.4}, setup)
    [c] = [c for c in pr.changes if c.group == "platform"]
    assert c.keys == ["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"]


def test_living_on_the_packers_is_left_to_the_model():
    pr = run({"bottoming_s": 0.0, "rh_min_fl": 12.0, "rh_min_fr": 12.0, "third_packers_f": 0.8}, _setup(THIRD))
    assert not [c for c in pr.changes if "PACKERS" in c.keys[0]]


def test_gt3_without_third_spring_is_unchanged():
    pr = run({"bottoming_s": 1.0, "rh_min_fl": 0.5, "rh_min_fr": 0.6}, _setup(THIRD_RH := {
        k: v for k, v in THIRD.items() if k.startswith("WM_")}))
    [c] = [c for c in pr.changes if c.group == "platform"]
    assert c.keys == ["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"] and THIRD_RH


def test_model_learns_third_spring_packers_and_dampers():
    dims = {d.name: d for d in build_dims(_setup(THIRD))}
    assert {"VM_FRONT_3RD_SPRING", "VM_FRONT_3RD_PACKERS", "VM_REAR_3RD_SLOWBUMP"} <= set(dims)
    assert group_of(dims["VM_FRONT_3RD_SPRING"]) == "third:F"
    assert group_of(dims["VM_REAR_3RD_SLOWBUMP"]) == "third:R"
