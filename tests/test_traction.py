"""TC a step a stint, each setting on its own signal: map from the exits, cut from how long
its events last, slip from countersteer on exit."""

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


MAP, CUT, SLIP = TC_KEYS


def _moves(changes):
    return {c.keys[0]: c.after[c.keys[0]] for c in changes}


def test_lots_of_short_tc_with_stable_exits_steps_map_and_cut_down():
    setup, scales = _car()
    tgt, m = _feel(0.45)
    tc = _traction(setup, scales, {"tc_s": 8.0, "tc_events": 100, "cs_exit": 1.0}, tgt, m, K, set())
    assert _moves(tc) == {MAP: 4, CUT: 4}  # some countersteer: slip stays
    assert all(c.group == "aids" and len(c.keys) == 1 for c in tc)
    assert "8.0 s a lap" in next(c for c in tc if c.keys == [MAP]).reason
    # And no countersteer on exit at all: slip comes down too.
    tc = _traction(setup, scales, {"tc_s": 8.0, "tc_events": 100, "cs_exit": 0.0}, tgt, m, K, set())
    assert _moves(tc) == {MAP: 4, CUT: 4, SLIP: 6}


def test_exit_oversteer_steps_the_map_up():
    setup, scales = _car()
    tgt, m = _feel(0.1)  # 0.3° under the target, band ±0.15
    tc = _traction(setup, scales, {"tc_s": 8.0, "tc_events": 100, "cs_exit": 1.0}, tgt, m, K, set())
    assert _moves(tc) == {MAP: 6}  # short events, oversteering exits: cut not lowered


def test_long_tc_events_step_the_cut_up():
    setup, scales = _car()
    tgt, m = _feel(0.45)
    tc = _traction(setup, scales, {"tc_s": 3.0, "tc_events": 10, "cs_exit": 1.0}, tgt, m, K, set())
    assert _moves(tc) == {CUT: 6} and "0.30 s" in tc[0].reason


def test_countersteer_on_exit_steps_the_slip_up():
    setup, scales = _car()
    tgt, m = _feel(0.45)
    tc = _traction(setup, scales, {"tc_s": 1.0, "tc_events": 20, "cs_exit": 3.0}, tgt, m, K, set())
    assert _moves(tc) == {SLIP: 8}


def test_settings_can_move_opposite_ways_in_one_stint():
    setup, scales = _car()
    tgt, m = _feel(0.45)
    tc = _traction(setup, scales, {"tc_s": 8.0, "tc_events": 100, "cs_exit": 3.0}, tgt, m, K, set())
    assert _moves(tc) == {MAP: 4, CUT: 4, SLIP: 8}


def test_otherwise_and_when_locked_no_change():
    setup, scales = _car()
    tgt, m = _feel(0.45)
    assert _traction(setup, scales, {"tc_s": 1.0, "tc_events": 20, "cs_exit": 1.0}, tgt, m, K, set()) == []
    busy = {"tc_s": 8.0, "tc_events": 100, "cs_exit": 0.0}
    assert _traction(setup, scales, busy, tgt, m, K, set(TC_KEYS)) == []
    assert _moves(_traction(setup, scales, busy, tgt, m, K, {CUT})) == {MAP: 4, SLIP: 6}


def test_a_linked_slip_map_follows_tc_and_is_not_moved():
    setup, scales = _car(slip_text="Linked")
    tgt, m = _feel(0.45)
    tc = _traction(setup, scales, {"tc_s": 8.0, "tc_events": 100, "cs_exit": 0.0}, tgt, m, K, set())
    assert _moves(tc) == {MAP: 4, CUT: 4}
