"""Why a proposal has no changes: the line tells a game-fixed setup from one locked here."""

from autosetup.feel.target import Target
from autosetup.lmu.setup_space import CarSetup
from autosetup.proposals import Plan, _settings_state, why_nothing
from autosetup.rules import Proposal

KEYS = ("VM_FRONT_ANTISWAY", "VM_REAR_ANTISWAY")


def _setup(available: bool) -> CarSetup:
    return CarSetup.from_json({k: {"available": available, "minValue": 0, "maxValue": 10, "value": 4,
                                   "stringValue": "4"} for k in KEYS})


def _why(setup: CarSetup, locks: set[str]) -> str:
    plan = Plan(1, "car", "GT3", "L", Target("car", "GT3", {}, {}), {}, searched=0,
                settings=_settings_state(setup, locks))
    return why_nothing(Proposal(1), plan, len(locks))["text"]


def test_a_fixed_setup_session_says_the_game_fixed_it():
    assert _why(_setup(False), set()).startswith("The game reports every setting as fixed")


def test_everything_locked_here_says_unlock():
    assert _why(_setup(True), set(KEYS)).startswith("Every setting the game lets you change is locked")


def test_a_car_without_balance_settings():
    assert _why(CarSetup.from_json({}), set()) == "This car has no balance settings the game lets you change"
