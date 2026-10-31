"""A stint without a full lap: asked about, and until it was fine, built from the last good one."""

from types import SimpleNamespace

from autosetup import analysis
from autosetup.proposals import Advisor


class _Store:
    def __init__(self, answer=None):
        self.answer = answer

    def recording(self, rid):
        return {"car_name": "Car", "track_layout": "L", "track": "L", "file_name": f"f{rid}"}

    def rating(self, name):
        return None if self.answer is None else {"undrivable": self.answer}


def _stints(monkeypatch):
    lap = {"complete": True, "in_pits": False}
    partial = {"complete": False, "in_pits": False}
    stints = [SimpleNamespace(id=1, laps=[lap]), SimpleNamespace(id=2, laps=[lap, lap]),
              SimpleNamespace(id=3, laps=[partial]), SimpleNamespace(id=4, laps=[{**lap, "in_pits": True}, partial])]
    monkeypatch.setattr(analysis, "stints", lambda store, car, layout: stints)


def _advisor(answer=None):
    adv = Advisor.__new__(Advisor)
    adv.store = _Store(answer)
    return adv


def test_a_short_stint_is_asked_about_and_the_last_good_one_is_the_base(monkeypatch):
    _stints(monkeypatch)
    assert _advisor().base_of(2) == (2, None)
    assert _advisor().base_of(4) == (2, {"stint": 4, "undrivable": None})  # unanswered
    assert _advisor(True).base_of(3) == (2, {"stint": 3, "undrivable": True})


def test_it_was_fine_builds_from_the_short_stint(monkeypatch):
    _stints(monkeypatch)
    assert _advisor(False).base_of(4) == (4, {"stint": 4, "undrivable": False})
