"""Hypercar-specific setup levers, split straight/corner aero platform, hybrid drag guard,
per-axle bottoming and [Hyper] class bands."""

from types import SimpleNamespace

import numpy as np

from autosetup.feel.measure import CELLS
from autosetup.knowledge import load_knowledge
from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.telemetry import Lap
from autosetup.metrics.signals import GRID_HZ
from autosetup.opt import bo
from autosetup.opt.balance_model import BalanceModel, Evidence
from autosetup.opt.laptime import project_platform
from autosetup.opt.platform import Platform as SafetyPlatform
from autosetup.opt.priors import sensitivities
from autosetup.opt.space import Dim, main_dims
from autosetup.rules import propose
from autosetup.scales import CarScales, Scale
from autosetup.sim.aero import AeroMap, Platform, Stint
from autosetup.sim.extract import extract
from autosetup.sim.model import StintData, _grouped_drag_deltas
from tests.test_aero_calibration import CIRCUIT, NO_EVENTS, drive_laps
from tests.test_lapsim import toy_car
from tests.test_rules import _setup


def _p(lo, hi, v, text):
    return {"available": True, "minValue": lo, "maxValue": hi, "value": v, "stringValue": text}


def test_brake_migration_is_main_lever_and_regen_is_pinned_to_max():
    setup = CarSetup.from_json({
        "VM_BRAKE_BALANCE": _p(0, 31, 16, "52.8:47.2"),
        "VM_BRAKE_MIGRATION": _p(0, 9, 2, "1.5% F"),
        "VM_REGEN_LEVEL": _p(0, 11, 5, "100kW"),
    })
    scales = CarScales({
        "VM_BRAKE_BALANCE": Scale("VM_BRAKE_BALANCE", {15: "53.0:47.0", 16: "52.8:47.2"}),
        "VM_BRAKE_MIGRATION": Scale("VM_BRAKE_MIGRATION", {1: "1.0% F", 2: "1.5% F"}),
        "VM_REGEN_LEVEL": Scale("VM_REGEN_LEVEL", {5: "100kW", 10: "200kW"}),
    })
    dims = main_dims(setup)
    assert [d.name for d in dims] == ["brake_bias", "brake_migration"]
    k = load_knowledge("Hyper")
    usage = {
        "braking_low": {"brake": 0.95, "throttle": 0.0},
        "braking_high": {"brake": 0.90, "throttle": 0.0},
        "entry_low": {"brake": 0.30, "throttle": 0.0},
        "entry_high": {"brake": 0.25, "throttle": 0.0},
    }
    s = sensitivities(dims, k, scales, usage)
    # Brake migration acts much harder under peak straight-line braking than during trail braking.
    assert s["brake_migration"]["braking_low"] > 2.0 * s["brake_migration"]["entry_low"] > 0
    # Regen is pushed straight to max by the rules and stays quiet once there.
    pr = propose(1, {}, setup, "Hyper", scales, knowledge=k)
    assert [c.after for c in pr.changes] == [{"VM_REGEN_LEVEL": 10}]
    at_max = CarSetup.from_json({"VM_REGEN_LEVEL": _p(0, 11, 10, "200kW")})
    assert propose(1, {}, at_max, "Hyper", scales, knowledge=k).changes == []


def test_coupled_brake_bias_and_migration_rotates_entry_while_holding_braking():
    dims = [
        Dim("brake_bias", ("VM_BRAKE_BALANCE",), 0, 20),
        Dim("brake_migration", ("VM_BRAKE_MIGRATION",), 0, 8),
    ]
    base = {"brake_bias": 10, "brake_migration": 2}
    scales = CarScales({
        "VM_BRAKE_BALANCE": Scale("VM_BRAKE_BALANCE", {9: "52.5:47.5", 10: "52.0:48.0"}),
        "VM_BRAKE_MIGRATION": Scale("VM_BRAKE_MIGRATION", {2: "1.0% F", 3: "1.5% F"}),
    })
    usage = {
        "braking_low": {"brake": 0.95, "throttle": 0.0},
        "braking_high": {"brake": 0.95, "throttle": 0.0},
        "entry_low": {"brake": 0.30, "throttle": 0.0},
        "entry_high": {"brake": 0.30, "throttle": 0.0},
    }
    slopes = sensitivities(dims, load_knowledge("Hyper"), scales, usage)
    now = {c: 0.5 for c in CELLS}
    ev = Evidence("L", bo.x_of(dims, base), {c: (v, 0.04) for c, v in now.items()}, True)
    model = BalanceModel.fit(slopes, "L", [ev])
    # Ask for more entry rotation while keeping straight-line braking stability tight.
    target = {**now, "entry_low": 0.25, "entry_high": 0.25}
    tol = {c: (0.08 if c.startswith("braking_") else 0.15) for c in CELLS}
    band = bo.TargetBand(target, tol, {c: 0.5 for c in CELLS}, 0.25)
    choice = bo.solve(bo.Problem(dims, base, lambda idx: (0.0, 5.0), model, band, seed=1))
    assert choice.moves
    # Rearward brake bias (higher index here because front % falls) + higher forward migration.
    assert choice.best.indices["brake_bias"] > base["brake_bias"]
    assert choice.best.indices["brake_migration"] > base["brake_migration"]


def test_per_axle_bottoming_and_packer_guard():
    plat = SafetyPlatform({"front": 20.0, "rear": 20.0}, bottoming_s=0.5, min_mm=2.0, bottoming_limit_s=0.2,
                          mm_per_click={"rh_front": 1.0, "rh_rear": 1.0}, bottoming_rear_s=0.0)
    base = {"rh_front": 10, "rh_rear": 10, "third_spring_front": 5, "third_spring_rear": 5,
            "third_packers_front": 4, "third_packers_rear": 4}
    assert not plat.ok(base, {**base, "rh_front": 9})
    assert not plat.ok(base, {**base, "third_spring_front": 4})
    assert not plat.ok(base, {**base, "third_packers_front": 3})
    assert plat.ok(base, {**base, "rh_rear": 9, "third_spring_rear": 4, "third_packers_rear": 3})


def test_split_straight_and_corner_ride_heights_in_extract_and_aero_map():
    n = 30 * GRID_HZ
    t = np.arange(n) / GRID_HZ
    part = np.arange(n) // (15 * GRID_HZ)  # 15 s straight, 15 s fast corner
    rh_f = np.where(part == 0, 28.0, 30.0)
    rh_r = np.where(part == 0, 32.0, 48.0)  # rear squats on the straight, holds rake in fast corners
    sig = SimpleNamespace(
        t=t,
        dist=np.linspace(0, 2000, n),
        speed=np.full(n, 60.0),
        a_lat=np.where(part == 0, 0.0, 1.8),
        a_lon=np.zeros(n),
        throttle=np.ones(n),
        brake=np.zeros(n),
        ride_height=np.column_stack([rh_f, rh_f, rh_r, rh_r]) / 1000.0,
    )
    sd = extract(NO_EVENTS, sig, [Lap(1, 0.0, 30.0, True, False)], {0})
    assert sd.rh_front == 28.0 and sd.rh_rear == 32.0
    assert sd.rh_front_corner == 30.0 and sd.rh_rear_corner == 48.0

    # AeroMap fits drag on straight-line rake and lift on fast-corner rake independently.
    k = load_knowledge("Hyper")
    stints = [
        Stint("M", Platform(0.5, 30.0, 32.0, 30.0, 40.0), (0.0, 1e-6), (0.0, 1e-6), None),
        Stint("M", Platform(0.5, 30.0, 38.0, 30.0, 40.0), (2e-5, 1e-6), (0.0, 1e-6), None),
        Stint("M", Platform(0.5, 30.0, 32.0, 30.0, 50.0), (0.0, 1e-6), (3e-5, 1e-6), None),
        Stint("M", Platform(0.5, 30.0, 38.0, 30.0, 50.0), (2e-5, 1e-6), (3e-5, 1e-6), None),
    ]
    amap = AeroMap.fit(stints, k)
    assert amap.drag.mean[2] > 2e-6
    assert amap.lift.mean[2] > 2e-6


def test_aero_map_allows_strong_evidence_of_diffuser_stall_to_flip_slope():
    k = load_knowledge("Hyper")
    # Prior says more rake adds lift (+), but 6 clean stints show strong stall (-).
    stints = [
        Stint("M", Platform(0.5, 30.0, 40.0 + 2.0 * i, 30.0, 40.0 + 2.0 * i),
              None, (-5e-6 * i, 5e-7), None)
        for i in range(6)
    ]
    amap = AeroMap.fit(stints, k)
    assert amap.lift.mean[2] < 0


def test_project_platform_applies_third_spring_heave_differently_on_straights_and_corners():
    ref = Platform(0.5, 30.0, 35.0, 31.0, 45.0)
    p = project_platform(ref, {"third_spring_rear": 4}, {"third_spring_rear": 2},
                         wing=None, mm_per_click={}, third_rh_mm=0.5, third_corner_rh_mm=0.3)
    assert p.rh_rear == 34.0  # -1.0 mm on straight (sheds drag)
    assert p.corner_rh_rear == 44.4  # only -0.6 mm in fast corners


def test_grouped_drag_deltas_isolates_hybrid_motor_maps():
    def stint(rid, motor_map, dk):
        sig, laps, _ = drive_laps(CIRCUIT, toy_car().perturb(d_drag=dk), seed=rid)
        rows = [(rid, b, n, v, a) for _, b, n, v, a in extract(NO_EVENTS, sig, laps, {0, 1, 2}).straights]
        setup = CarSetup.from_json({"VM_ELECTRIC_MOTOR_MAP": _p(0, 10, motor_map, f"{motor_map * 20}kW")})
        return StintData(rid, f"s{rid}", setup, None, [], rows, [], None, [0, 1, 2])

    # Stints 1 and 2 share motor map 6 (real drag diff); stint 3 is alone on motor map 9.
    drag = _grouped_drag_deltas([stint(1, 6, 0.0), stint(2, 6, 1.2e-4), stint(3, 9, -2.0e-4)])
    assert 1 in drag and 2 in drag and 3 not in drag
    assert drag[2][0] > drag[1][0]


def test_hyper_per_axle_brake_temp_and_rear_camber_bands():
    k = load_knowledge("Hyper")
    setup = _setup(extra={
        "VM_BRAKE_DUCTS": _p(0, 6, 1, "Open"),
        "VM_BRAKE_DUCTS_REAR": _p(0, 6, 1, "Open"),
        "WM_CAMBER-W_RL": _p(0, 40, 20, "-1.0 deg"),
        "WM_CAMBER-W_RR": _p(0, 40, 20, "-1.0 deg"),
    })
    scales = CarScales({
        "WM_CAMBER-W_RL": Scale("WM_CAMBER-W_RL", {19: "-1.1 deg", 20: "-1.0 deg"}),
        "WM_CAMBER-W_RR": Scale("WM_CAMBER-W_RR", {19: "-1.1 deg", 20: "-1.0 deg"}),
    })
    # Rear discs at 330 °C (cool from hybrid regen) and rear spread at 4 °C are both inside [Hyper].
    pr = propose(1, {"brake_temp_max_fl": 420.0, "brake_temp_max_fr": 420.0,
                     "brake_temp_max_rl": 330.0, "brake_temp_max_rr": 330.0,
                     "t_spread_rl": 4.0, "t_spread_rr": 4.0},
                 setup, "Hyper", scales, knowledge=k)
    assert pr.changes == []
    # Front discs at 330 °C ARE below Hyper's front floor (380 °C) and close front ducts.
    pr2 = propose(1, {"brake_temp_max_fl": 330.0, "brake_temp_max_fr": 330.0,
                      "brake_temp_max_rl": 330.0, "brake_temp_max_rr": 330.0},
                  setup, "Hyper", scales, knowledge=k)
    assert [c.keys for c in pr2.changes] == [["VM_BRAKE_DUCTS"]]


def test_lmp2_elms_overrides_lmp2_aero_and_isolates_stints_by_class():
    import json
    from autosetup.sim.model import stints_of
    from autosetup.store import Store

    wec = load_knowledge("LMP2")
    elms = load_knowledge("LMP2_ELMS")
    assert elms["brake_pressure_pct"] == wec["brake_pressure_pct"]
    assert elms["lift_c_typ"] > wec["lift_c_typ"]
    assert elms["drag_k_typ"] > wec["drag_k_typ"]

    st = Store(":memory:")
    sj = json.dumps({"VM_REAR_WING": _p(0, 10, 4, "4 deg")})
    for i, cls in enumerate(("LMP2", "LMP2_ELMS"), start=1):
        st.con.execute(
            """insert into recordings (id, file_name, file_size, file_mtime, track, track_layout,
                   car_name, car_class, car_signature, setup_json, rh_front_fast, rh_rear_fast)
               values (?, ?, 1, ?, 'Spa', 'Spa', 'Oreca', ?, 'same_sig', ?, 30.0, 45.0)""",
            [i, f"f{i}.duckdb", float(i), cls, sj],
        )
    assert [s.rid for s in stints_of(st, "same_sig", car_class="LMP2")] == [1]
    assert [s.rid for s in stints_of(st, "same_sig", car_class="LMP2_ELMS")] == [2]


def test_telemetry_lockup_detection_and_front_vs_rear_brake_rule():
    from autosetup.metrics.compute import _driver
    from autosetup.proposals import Advisor
    from autosetup.store import Store
    import json

    n = 6 * GRID_HZ
    t = np.arange(n) / GRID_HZ
    brake = np.full(n, 0.9)
    # Decel holds at 2.5 g then suddenly drops to 1.4 g between 2.0 s and 2.5 s while FL surface temp spikes.
    a_lon = np.full(n, -2.5)
    lock_mask = (t >= 2.0) & (t <= 2.5)
    a_lon[lock_mask] = np.linspace(-2.5, -1.4, lock_mask.sum())
    a_lon[t > 2.5] = -1.4
    tc = np.full((n, 4), 85.0)
    tc[lock_mask, 0] = np.linspace(85.0, 110.0, lock_mask.sum())  # +50 °C/s front-left spike
    tc[t > 2.5, 0] = 110.0
    sig = SimpleNamespace(
        t=t, speed=np.full(n, 50.0), brake=brake, throttle=np.zeros(n),
        a_lat=np.zeros(n), a_lon=a_lon, tyre_centre=tc,
    )
    m = _driver(sig)
    assert m["lockup_front"] >= 1.0 and m["lockup_rear"] == 0.0
    assert m["brake_sat_frac"] == 0.0

    # Front lock-ups lower VM_BRAKE_PRESSURE; rear-only lock-ups emit a bias/migration note instead.
    setup = _setup(extra={
        "VM_BRAKE_PRESSURE": _p(0, 81, 76, "116 kgf (97%)"),
        "VM_ANTILOCKBRAKESYSTEMMAP": _p(0, 1, 0, "N/A"),
    })
    k = load_knowledge("Hyper")
    pr_f = propose(1, {"lockup_front": 2.0, "lockup_rear": 0.0}, setup, "Hyper", CarScales({}), knowledge=k)
    assert [c.after for c in pr_f.changes if c.keys == ["VM_BRAKE_PRESSURE"]] == [{"VM_BRAKE_PRESSURE": 75}]
    pr_r = propose(1, {"lockup_front": 0.0, "lockup_rear": 2.0}, setup, "Hyper", CarScales({}), knowledge=k)
    assert not [c for c in pr_r.changes if c.keys == ["VM_BRAKE_PRESSURE"]]
    assert any("Rear lock-ups" in note for note in pr_r.notes)

    # Brake ceiling is scoped per layout so Monza lockups don't cap Spa.
    st = Store(":memory:")
    adv = Advisor(st, None)
    sj = json.dumps({"VM_BRAKE_PRESSURE": _p(0, 81, 76, "116 kgf (97%)")})
    st.con.execute(
        """insert into recordings (id, file_name, file_size, file_mtime, track, track_layout,
               car_name, car_class, car_signature, setup_json)
           values (1, 'monza.duckdb', 1, 1.0, 'Monza', 'Monza', '9X8', 'Hyper', 'sig1', ?)""", [sj])
    st.con.execute("insert into ratings (file_name, lockups) values ('monza.duckdb', true)")
    assert adv.brake_ceiling("sig1", "Hyper", "Monza") == 75
    assert adv.brake_ceiling("sig1", "Hyper", "Spa") is None
