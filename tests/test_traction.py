"""TC a step a stint, map, cut and slip together, from how much it works and the exits."""

from autosetup.feel.measure import Measured
from autosetup.feel.target import Target
from autosetup.knowledge import load_knowledge
from autosetup.lmu.setup_space import CarSetup
from autosetup.metrics.balance import Cell
from autosetup.proposals import TC_KEYS, _traction
from autosetup.scales import CarScales, Scale

K = load_knowledge("GT3")


def _car(map_=5, cut=5, slip=7, slip_text=None):
    def p(v, text=None):
        return {"available": True, "minValue": 0, "maxValue": 12, "value": v, "stringValue": text or str(v)}
    setup = CarSetup.from_json(dict(zip(TC_KEYS, (p(map_), p(cut), p(slip, slip_text)))))
    scales = CarScales({key: Scale(key, {i: str(i) for i in range(12)}) for key in TC_KEYS})
    return setup, scales


def _feel(exit_measured: float, target: float = 0.4, tol: float = 0.15):
    cells = {"exit_low": target, "exit_high": target}
    tgt = Target("car", "GT3", cells, {c: tol for c in cells}, weights={c: 0.5 for c in cells})
    return tgt, Measured({c: Cell(exit_measured, 0.03, 20) for c in cells})


def test_lots_of_tc_with_stable_exits_steps_all_three_down():
    setup, scales = _car()
    tgt, m = _feel(0.45)
    tc = _traction(setup, scales, {"tc_s": 8.0, "cs_exit": 0.5}, tgt, m, K, set())
    assert tc.group == "aids" and tc.name == "TC"
    assert tc.after == {TC_KEYS[0]: 4, TC_KEYS[1]: 4, TC_KEYS[2]: 6}
    assert "8.0 s a lap" in tc.reason


def test_exit_oversteer_steps_all_three_up():
    setup, scales = _car()
    tgt, m = _feel(0.1)  # 0.3° under the target, band ±0.15
    tc = _traction(setup, scales, {"tc_s": 8.0, "cs_exit": 0.5}, tgt, m, K, set())
    assert tc.after == {TC_KEYS[0]: 6, TC_KEYS[1]: 6, TC_KEYS[2]: 8} and tc.reason == "oversteer on exit"
    tgt, m = _feel(0.45)  # in the band, but catching the rear on exit
    tc = _traction(setup, scales, {"tc_s": 1.0, "cs_exit": 3.0}, tgt, m, K, set())
    assert tc.delta == 1


def test_otherwise_and_when_locked_no_change():
    setup, scales = _car()
    tgt, m = _feel(0.45)
    assert _traction(setup, scales, {"tc_s": 1.0, "cs_exit": 0.5}, tgt, m, K, set()) is None
    assert _traction(setup, scales, {"tc_s": 8.0, "cs_exit": 0.5}, tgt, m, K, set(TC_KEYS)) is None
    # One key locked: the other two still move.
    tc = _traction(setup, scales, {"tc_s": 8.0, "cs_exit": 0.5}, tgt, m, K, {TC_KEYS[2]})
    assert set(tc.keys) == set(TC_KEYS[:2])


def test_a_linked_slip_map_follows_tc_and_is_not_moved():
    setup, scales = _car(slip_text="Linked")
    tgt, m = _feel(0.45)
    tc = _traction(setup, scales, {"tc_s": 8.0, "cs_exit": 0.5}, tgt, m, K, set())
    assert set(tc.keys) == set(TC_KEYS[:2])
