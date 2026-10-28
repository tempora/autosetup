import numpy as np
import pytest

from autosetup.sim.aero import AeroMap, Platform, Slopes
from autosetup.sim.lapsim import G, V_GRID, Car, Track, corner_speed, fit_eta, simulate
from autosetup.sim.sensitivity import aero_sensitivity


def toy_car(mu=1.6, lift=6e-5, drag=7e-4, power_a=9.0, mu_brake=1.8) -> Car:
    """Grip μ·g·(1 + c·v²); drive a falling line minus drag; braking with downforce + drag."""
    v = V_GRID
    return Car(lat=mu * G * (1 + lift * v ** 2), drive=power_a * np.exp(-v / 45.0) + 1.0 - drag * v ** 2,
               brake=mu_brake * G * (1 + lift * v ** 2) + drag * v ** 2, mu=mu, mu_brake=mu_brake)


def track(*pieces: tuple[float, float]) -> Track:
    """(length m, radius m; 0 = straight) pieces, 5 m steps."""
    kappa = np.concatenate([np.full(int(length / 5), 0.0 if r == 0 else 1.0 / r) for length, r in pieces])
    return Track(5.0, kappa)


def test_steady_corner_speed():
    car = toy_car(lift=0.0)
    vc = corner_speed(car, np.array([1 / 50.0, 1 / 200.0]))
    assert vc == pytest.approx(np.sqrt(1.6 * G * np.array([50.0, 200.0])), abs=0.6)  # 0.5 m/s speed grid


def test_top_speed_is_where_drive_runs_out():
    car = toy_car()
    res = simulate(track((6000, 0), (200, 40)), car)
    assert res.v.max() == pytest.approx(car.top_speed, abs=1.0)


def test_braking_follows_the_brake_curve():
    car = toy_car(lift=0.0, drag=0.0)
    res = simulate(track((3000, 0), (300, 30)), car)
    v = res.v[:600]
    braking = np.flatnonzero(np.diff(v) < -1e-6)
    i = braking[len(braking) // 2]
    decel = (v[i] ** 2 - v[i + 1] ** 2) / (2 * 5.0)
    assert decel == pytest.approx(car.at(car.brake, v[i + 1]), rel=0.05)


def test_drag_and_downforce_move_lap_time_the_right_way():
    fast = track((1500, 0), (400, 300), (800, 0), (200, 60), (600, 0), (300, 400))
    car = toy_car()
    base = simulate(fast, car).time
    assert simulate(fast, car.perturb(d_drag=1e-4)).time > base
    assert simulate(fast, car.perturb(d_lift=2e-5)).time < base
    assert simulate(fast, car.perturb(d_drag=1e-4)).v.max() < simulate(fast, car).v.max()


def test_wing_pays_on_twisty_tracks_and_costs_on_fast_ones():
    """Same car and aero map: more wing (drag and downforce) is quicker where the lap is
    corners and slower where it is straights."""
    aero = AeroMap(drag=Slopes(np.array([1.5e-4, 0, 0]), np.eye(3) * 1e-12),
                   lift=Slopes(np.array([3e-5, 0, 0]), np.eye(3) * 1e-12),
                   balance=Slopes(np.zeros(3), np.eye(3)))

    class Model:  # what aero_sensitivity uses of a LayoutModel
        def __init__(self, t):
            self.track, self.base, self.platform, self.eta = t, toy_car(), Platform(0.5, 40, 50), 1.0

        def car_at(self, a, p):
            return a.car(self.base, self.platform, p)

        def simulate(self, car):
            return simulate(self.track, car)

    twisty = track(*[(150, 0), (200, 80), (150, 0), (250, 120), (100, 0), (300, 150)] * 4)
    flat_out = track((2500, 0), (150, 40), (2500, 0), (150, 40))
    p = Platform(0.5, 40, 50)
    assert aero_sensitivity(Model(twisty), aero, p, 9).wing_s_per_click < 0
    assert aero_sensitivity(Model(flat_out), aero, p, 9).wing_s_per_click > 0


def test_driver_efficiency_is_recovered():
    t = track((800, 0), (200, 60), (500, 0), (300, 150), (400, 0), (150, 40))
    car = toy_car()
    laps = [(t, car, simulate(t, car.scaled(0.91)).time)]
    assert fit_eta(laps) == pytest.approx(0.91, abs=0.005)


def test_lap_time_is_smooth_in_downforce_even_with_a_staircase_envelope():
    """Noisy envelope bins (a staircase once made non-decreasing) and a fast corner whose
    need runs along the steps: lap time must still move smoothly with downforce, or the
    optimiser's finite differences read jumps as sensitivities."""
    from autosetup.sim.aero import envelope_car

    rng = np.random.default_rng(4)
    env = [("lat", v, 200, 16.0 * (1 + 6e-5 * v * v) + rng.normal(0, 0.8)) for v in np.arange(17.5, 70, 5.0)]
    env += [("drive", v, 200, 9.0 * np.exp(-v / 45.0) + 1.0 - 7e-4 * v * v) for v in np.arange(17.5, 80, 5.0)]
    env += [("brake", v, 200, 18.0 + 0.002 * v * v) for v in np.arange(22.5, 80, 5.0)]
    car = envelope_car(env, 6e-5)
    t = track((1200, 0), (300, 110), (900, 0), (200, 45), (600, 0), (250, 160))
    times = np.array([simulate(t, car.perturb(d_lift=x * 2e-7)).time for x in range(-20, 21)])
    assert np.abs(np.diff(times, 2)).max() < 0.002
    assert np.all(np.diff(times) <= 1e-9)  # more downforce is never slower on this track
