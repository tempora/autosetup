import numpy as np
import pytest

from autosetup.metrics.balance import PhaseObs, cells, normalise, observe, sample_metrics
from autosetup.metrics.corners import assign, corner_map, passes, track_corners
from autosetup.metrics.signals import GRID_HZ, G, Signals
from autosetup.lmu.telemetry import Lap

GEOMETRY = (270.0, 15.0, 2.7)  # half lock (deg), steering ratio, wheelbase (m)


def make_signals(n: int, **arrays) -> Signals:
    nan4 = np.full((n, 4), np.nan)
    base = dict(t=np.arange(n) / GRID_HZ, dist=np.zeros(n), speed=np.full(n, 30.0), steer=np.zeros(n),
                throttle=np.zeros(n), brake=np.zeros(n), yaw_rate=np.zeros(n), a_lat=np.zeros(n), a_lon=np.zeros(n),
                ride_height=nan4, off_line=np.full(n, -5.0), surface=np.zeros((n, 4)), tc=np.zeros(n),
                abs=np.zeros(n), tyre_inner=nan4, tyre_centre=nan4, tyre_outer=nan4, tyre_pressure=nan4,
                tyre_carcass=nan4, tyre_wear=nan4, brake_temp=nan4, overheating=np.zeros(n), fuel=np.zeros(n),
                track_temp=np.zeros(n), ambient_temp=np.zeros(n), third=np.full((n, 2), np.nan))
    base.update(arrays)
    return Signals(**base)


def bicycle(speed: np.ndarray, radius: np.ndarray, gradient_deg_per_g: float, direction: int = 1) -> dict:
    """Steady-state bicycle model: road-wheel angle = L/R + K·a_y (K in rad per g)."""
    half_lock, ratio, wb = GEOMETRY
    a_y = speed ** 2 / radius / G
    delta = wb / radius + np.radians(gradient_deg_per_g) * a_y
    return dict(speed=speed, yaw_rate=direction * speed / radius, a_lat=direction * a_y,
                steer=direction * np.degrees(delta) * ratio / half_lock)


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("gradient", [-0.5, 0.0, 1.5])
def test_understeer_angle_recovers_the_gradient(gradient, direction):
    """A car with understeer gradient K reads ua = K·a_y, + for understeer, either way round,
    including an oversteering car held with opposite lock at the top of the speed range."""
    n = 200
    speed = np.linspace(20, 60, n)
    radius = np.full(n, 150.0)
    sig = make_signals(n, **bicycle(speed, radius, gradient, direction))
    per = sample_metrics(sig, GEOMETRY)
    a_y = speed ** 2 / radius / G
    assert np.allclose(per["ua"], gradient * a_y, atol=1e-6)
    if gradient:  # the share is undefined while the wheel passes straight ahead
        ye = per["yaw_err"][np.isfinite(per["yaw_err"])]
        assert len(ye) > n // 2 and np.all(np.sign(ye) == np.sign(gradient))


def _track_laps(n_laps: int, gradient: float, noise: float = 0.0, seed: int = 0):
    """Laps of a 2 km track: a right-hander braked into at ~500 m and a left hairpin at
    ~1400 m, at a constant 50 m/s elsewhere."""
    rng = np.random.default_rng(seed)
    lap_len, dt = 2000.0, 1 / GRID_HZ
    d_grid = np.arange(0, lap_len, 1.0)
    v = np.full_like(d_grid, 50.0)
    radius = np.full_like(d_grid, 1e6)
    for apex, r, v_apex, sign in ((550.0, 120.0, 30.0, 1), (1400.0, 30.0, 15.0, -1)):
        corner = np.abs(d_grid - apex) < 60
        radius[corner] = sign * r
        brake_zone = (d_grid > apex - 160) & (d_grid < apex)
        v = np.minimum(v, np.where(brake_zone, v_apex + (apex - d_grid) * 0.12, v))
        v[corner & (d_grid >= apex)] = np.minimum(v[corner & (d_grid >= apex)], v_apex + (d_grid[corner & (d_grid >= apex)] - apex) * 0.1)
        v[corner & (d_grid < apex)] = np.minimum(v[corner & (d_grid < apex)], v_apex + (apex - d_grid[corner & (d_grid < apex)]) * 0.1)
    t_lap = np.concatenate([[0], np.cumsum(1.0 / v[:-1])])
    T = t_lap[-1] + 1.0 / v[-1]
    t = np.arange(0, n_laps * T, dt)
    lap_no = (t // T).astype(int)
    tl = t - lap_no * T
    d = np.interp(tl, t_lap, d_grid)
    speed = np.interp(d, d_grid, v)
    r = np.interp(d, d_grid, radius)
    sign = np.sign(r)
    rad = np.abs(r)
    model = bicycle(speed, rad, gradient, 1)
    steer = sign * model["steer"] + rng.normal(0, noise, len(t))
    accel = np.gradient(speed, dt)
    sig = make_signals(len(t), t=t, dist=d, speed=speed, steer=steer, yaw_rate=sign * model["yaw_rate"],
                       a_lat=sign * model["a_lat"], brake=np.where(accel < -2, 0.8, 0.0),
                       throttle=np.where(accel > 0.5, 1.0, np.where(accel < -2, 0.0, 0.3)))
    laps = [Lap(k + 1, k * T, (k + 1) * T, True, False) for k in range(n_laps)]
    return sig, laps


def test_corner_map_finds_both_corners_with_direction_and_phases():
    sig, laps = _track_laps(3, 1.0)
    cmap = corner_map(sig, laps)
    assert [c.direction for c in cmap] == [1, -1]
    assert abs(cmap[0].d_apex - 550) < 40 and abs(cmap[1].d_apex - 1400) < 40
    assert cmap[0].d_start < cmap[0].d_apex - 60  # braking zone counted as entry
    m = (sig.t >= laps[0].t_start) & (sig.t < laps[0].t_end)
    sl = Signals(**{k: v[m] for k, v in vars(sig).items()})
    ps = passes(sl, cmap, 140 / 3.6)
    assert [p.band for p in ps] == ["low", "low"]
    for p in ps:
        assert {"entry", "exit"} <= set(p.phase)


def test_pass_observations_carry_the_gradient():
    sig, laps = _track_laps(2, 1.2)
    cmap = corner_map(sig, laps)
    per = sample_metrics(sig, GEOMETRY)
    obs = []
    for lap in laps:
        m = (sig.t >= lap.t_start) & (sig.t < lap.t_end)
        sl = Signals(**{k: v[m] for k, v in vars(sig).items()})
        for p in passes(sl, cmap, 140 / 3.6):
            obs += observe(sl, {k: v[m] for k, v in per.items()}, p)
    assert obs
    for o in obs:
        assert o.ua == pytest.approx(1.2 * o.a_lat, rel=0.15, abs=0.05)


def _obs(corner, ua, steer_rate, throttle_rate=None, n=25, var=0.01, phase="mid", band="low"):
    return PhaseObs(corner=corner, phase=phase, band=band, n=n, v_min=30.0, v_mean=32.0, a_lat=1.5, ua=ua,
                    ua_var=var, yaw_err=0.1, steer_rate=steer_rate, throttle_rate=throttle_rate,
                    countersteer_s=0.0)


def test_normalise_takes_steering_rate_out():
    """Passes where you turned in faster read more understeer; normalised, they don't."""
    rng = np.random.default_rng(1)
    obs = []
    for corner, base in ((0, 0.8), (1, 1.4), (2, 0.3)):
        for _ in range(12):
            sr = rng.normal(100, 30)
            obs.append(_obs(corner, base + 0.01 * (sr - 100) + rng.normal(0, 0.02), sr))
    adj = normalise(obs)
    for corner, base in ((0, 0.8), (1, 1.4), (2, 0.3)):
        raw = np.array([o.ua for o in obs if o.corner == corner])
        new = np.array([o.value for o in adj if o.corner == corner])
        usual = np.median([o.steer_rate for o in obs if o.corner == corner])
        assert np.std(new) < 0.5 * np.std(raw)
        # Each pass is moved to the corner's usual steering rate.
        assert abs(np.mean(new) - (base + 0.01 * (usual - 100))) < 0.03


def test_normalise_leaves_few_passes_alone():
    obs = [_obs(0, 1.0, 50.0), _obs(0, 1.2, 150.0)]
    assert [o.value for o in normalise(obs)] == [1.0, 1.2]


def test_cells_weight_by_count_and_variance():
    precise = [_obs(c, 1.0, 100, var=0.0004) for c in range(10)]
    noisy = [_obs(10, 3.0, 100, var=1.0)]
    cell = cells(precise + noisy)[("mid", "low")]
    assert cell.passes == 11
    assert abs(cell.mean - 1.0) < 0.25  # the noisy pass barely moves it
    one = cells([_obs(0, 1.0, 100, var=0.0004)])[("mid", "low")]
    many = cells(precise)[("mid", "low")]
    assert one.se > 2 * many.se  # more passes, more certain: no gate, just weight
    assert one.se >= 0.1  # but one pass is never taken as certain


def test_countersteer_reads_as_oversteer():
    """Wheel pointing out of a right-hander the car is rotating through: oversteer."""
    n = 50
    sig = make_signals(n, speed=np.full(n, 30.0), yaw_rate=np.full(n, 0.3), a_lat=np.full(n, 0.9),
                       steer=np.full(n, -0.05))
    assert np.all(sample_metrics(sig, GEOMETRY)["ua"] < 0)


# -- the track's corners: voted from the recordings' own ----------------------------------
def test_a_corner_only_one_of_three_stints_has_is_not_a_track_corner():
    """A spin or a weave on one stint's only lap shows up as a corner there alone."""
    real = [(100.0, 300.0, 1), (500.0, 700.0, -1)]
    track = track_corners([real, real + [(900.0, 960.0, 1)], real])
    assert [(c.d_start, c.d_end, c.direction) for c in track] == [(100.0, 300.0, 1), (500.0, 700.0, -1)]
    assert assign(900.0, 960.0, 1, track) is None


def test_a_corner_missing_from_the_first_stint_is_still_a_track_corner():
    """No first-recording list: two stints out of three are enough."""
    real = [(100.0, 300.0, 1)]
    track = track_corners([real, real + [(500.0, 700.0, -1)], real + [(505.0, 710.0, -1)]])
    assert len(track) == 2
    assert assign(500.0, 700.0, -1, track) == track[1].id


def test_a_stint_without_a_full_lap_does_not_vote_but_is_numbered(tmp_path):
    from autosetup.feel.measure import passes_of
    from autosetup.store import Store

    store = Store(tmp_path / "db.duckdb")
    con = store.con
    for rid, complete, own in ((1, True, [(100.0, 300.0, 1)]), (2, True, [(110.0, 290.0, 1)]),
                               (3, False, [(100.0, 300.0, 1), (500.0, 700.0, -1)])):
        con.execute("insert into recordings (id, file_name, file_size, file_mtime, track, track_layout) values (?, ?, 1, 1, 'T', 'T')",
                    [rid, f"r{rid}.duckdb"])
        con.execute("insert into laps (recording_id, seq, complete, in_pits) values (?, 0, ?, false)",
                    [rid, complete])
        for k, (s, e, d) in enumerate(own):
            con.execute("""insert into corner_passes (recording_id, seq, corner, phase, band, n, v_min, v_mean,
                           a_lat, ua, ua_var, yaw_err, steer_rate, countersteer_s, kerb, d_start, d_end, direction)
                           values (?, 0, ?, 'mid', 'low', 20, 30, 32, 1.5, 0.5, 0.01, 0.1, 50, 0, false, ?, ?, ?)""",
                        [rid, k, s, e, d])
    assert len(store.track_corners("T")) == 1  # the partial lap's second corner doesn't count
    rows = passes_of(store, [1, 2, 3])
    assert {(rid, o.corner) for rid, _, o in rows} == {(1, 0), (2, 0), (3, 0)}


def test_one_corner_whose_slowest_point_moves_500_m_gets_one_number():
    """A long fast corner: its slowest point sits at the entry in one stint and the exit
    in another (Blanchimont). Numbered by overlap, it's the same corner."""
    track = track_corners([[(5550.0, 6180.0, -1)], [(5600.0, 6175.0, -1)]])
    assert len(track) == 1
    assert assign(5550.0, 6180.0, -1, track) == assign(5600.0, 6175.0, -1, track) == track[0].id


def test_two_corners_another_stint_merges_stay_sensible():
    """One stint sees a left-left as two corners, the others as one: no crash, every pass
    of them gets a number."""
    two = [(100.0, 200.0, -1), (240.0, 340.0, -1)]
    one = [(100.0, 340.0, -1)]
    track = track_corners([two, one, one])
    assert track
    for s, e, d in two + one:
        assert assign(s, e, d, track) is not None


def test_no_votes_no_track_corners():
    assert track_corners([]) == []
    assert track_corners([[], []]) == []
