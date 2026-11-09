from datetime import datetime

import numpy as np

from autosetup.lmu import motec

HEADER_SIZE = motec._HEADER.size
CHANNEL_SIZE = motec._CHANNEL.size


def write_ld(path, channels: dict[str, tuple], venue: str = "Test Raceway") -> None:
    """A minimal .ld: header, linked channel metadata, float32 data (DAM's layout). A channel
    is (freq, values, unit), or (freq, raw int16 values, unit, (shift, mul, scale, dec)) for
    DAM's scaled integer channels."""
    names = list(channels)
    meta_at = HEADER_SIZE
    data_at = meta_at + CHANNEL_SIZE * len(names)
    blobs, metas, offset = [], [], data_at
    for i, name in enumerate(names):
        freq, values, unit, *enc = channels[name]
        if enc:
            raw, kind, conv = np.asarray(values, dtype="<i2").tobytes(), (0x03, 2), enc[0]
        else:
            raw, kind, conv = np.asarray(values, dtype="<f4").tobytes(), (0x07, 4), (0, 1, 1, 0)
        prev = meta_at + CHANNEL_SIZE * (i - 1) if i else 0
        nxt = meta_at + CHANNEL_SIZE * (i + 1) if i + 1 < len(names) else 0
        metas.append(motec._CHANNEL.pack(prev, nxt, offset, len(values), 0, *kind, freq, *conv,
                                         name.encode(), name[:8].encode(), unit.encode()))
        blobs.append(raw)
        offset += len(raw)
    header = motec._HEADER.pack(0x40, meta_at, data_at, 0, 1, 0x4240, 0xF, 1, b"ADL", 420, 0xADB0, len(names),
                                b"01/01/2026", b"10:00:00", b"Driver", b"Car", venue.encode(), 0, b"")
    path.write_bytes(header + b"".join(metas) + b"".join(blobs))


def test_read_channels_and_clock(tmp_path):
    t = np.arange(0, 60, 0.02)
    write_ld(tmp_path / "x.ld", {
        motec.CLOCK: (50, np.floor(1000 + t), "s"),
        "Tyre Load FL": (10, np.full(600, 3200.0), "N"),
        "Ground Speed": (10, np.linspace(100, 200, 600), "km/h"),
    })
    log = motec.MotecLog(tmp_path / "x.ld")
    assert log.venue == "Test Raceway" and set(log.channels) == {motec.CLOCK, "Tyre Load FL", "Ground Speed"}
    assert log.clock_span() == (1000.0, 1059.0)
    tt, v = log.channel("Tyre Load FL")
    assert len(v) == 600 and np.allclose(v, 3200.0) and tt[0] == 1000.0 and abs(tt[1] - tt[0] - 0.1) < 1e-9


def test_scaled_integer_channel_adds_shift_after_mul(tmp_path):
    """DAM's int16 channels: value = raw / scale * 10^-dec * mul + shift (shift not scaled by mul).
    Ground Speed as DAM encodes it: shift 641, mul 2, dec 2, so raw -32050 is 0 km/h."""
    raw = np.array([-32050, -24550, -17050])
    write_ld(tmp_path / "z.ld", {motec.CLOCK: (1, np.array([0.0, 1.0, 2.0]), "s"),
                                 "Ground Speed": (1, raw, "km/h", (641, 2, 1, 2))})
    v = motec.MotecLog(tmp_path / "z.ld").values("Ground Speed")
    assert np.allclose(v, [0.0, 150.0, 300.0])


def test_blank_channels_are_left_out(tmp_path):
    """LMU writes zero tyre load for GT3s: that's no data, not zero load."""
    n = 100
    chans = {motec.CLOCK: (10, np.floor(500 + np.arange(n) / 10), "s"), "Drag": (10, np.zeros(n), "N")}
    for w in motec.WHEELS:
        chans[f"Tyre Load {w}"] = (10, np.zeros(n), "N")
        chans[f"Lat Ground Vel {w}"] = (10, np.linspace(-2, 2, n), "m/s")
    write_ld(tmp_path / "g.ld", chans)
    out = motec.resample(motec.MotecLog(tmp_path / "g.ld"), np.linspace(501, 508, 50))
    assert set(out) == {"lat_ground_vel"}


def test_whole_second_clock_is_refined_from_ground_speed(tmp_path):
    """DAM logs its session clock in whole seconds; Ground Speed in both logs lines them up."""
    true_start = 1000.37
    t_log = np.arange(0, 120, 0.1)
    speed = 150 + 60 * np.sin(t_log / 7.0) + 20 * np.sin(t_log / 2.3)
    write_ld(tmp_path / "y.ld", {motec.CLOCK: (10, np.floor(true_start + t_log), "s"),
                                 "Ground Speed": (10, speed, "km/h")})
    log = motec.MotecLog(tmp_path / "y.ld")
    t_rec = np.arange(1010, 1100, 0.02)
    rec_speed = np.interp(t_rec, true_start + t_log, speed)
    offset = motec.clock_offset(log, t_rec, rec_speed)
    assert abs(offset - 0.37) < 0.03


def test_find_log_by_track_time_and_overlap(tmp_path):
    t = np.arange(0, 100, 0.1)
    write_ld(tmp_path / "2026-10-30 - 14-00-00 - Test Raceway - P1.ld", {motec.CLOCK: (10, 500 + t, "s")})
    write_ld(tmp_path / "2026-10-30 - 14-00-00 - Other Track - P1.ld", {motec.CLOCK: (10, 500 + t, "s")})
    write_ld(tmp_path / "2026-10-20 - 14-00-00 - Test Raceway - P1.ld", {motec.CLOCK: (10, 500 + t, "s")})
    near = datetime(2026, 10, 30, 14, 0, 30)
    found = motec.find_log(tmp_path, "Test Raceway", 510.0, 590.0, near)
    assert found is not None and found.path.name.startswith("2026-10-30") and "Test Raceway" in found.path.name
    assert motec.find_log(tmp_path, "Test Raceway", 900.0, 990.0, near) is None  # another session's clock
