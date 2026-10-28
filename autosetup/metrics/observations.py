"""Everything the models learn from, per recording: corner-pass balance observations,
per-lap suspension histograms and stats, kerb events.

Passes come from every lap outside the pits, partial laps included: a corner taken at
speed is evidence whether or not the lap was finished. Passes far slower than the
recording's usual pass of that corner (traffic, a mistake, an out-lap) are left out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from autosetup.knowledge import load_knowledge
from autosetup.lmu import motec
from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.telemetry import Lap, Session
from autosetup.metrics import balance, corners, kerbs, suspension
from autosetup.metrics.compute import steering_geometry
from autosetup.metrics.signals import Signals
from autosetup.sim.extract import SimData
from autosetup.sim.extract import extract as sim_extract

SLOW_PASS = 0.9  # passes under this share of the corner's median speed are left out


@dataclass
class Observations:
    corners: list[corners.Corner] = field(default_factory=list)
    passes: list[tuple[int, balance.PhaseObs]] = field(default_factory=list)  # (lap seq, obs)
    suspension: list[suspension.LapSuspension] = field(default_factory=list)
    kerbs: list[tuple[int, kerbs.KerbEvent]] = field(default_factory=list)  # (lap seq, event)
    dam: str | None = None  # file name of the DAM log used
    load_source: str | None = None
    kerb_source: str | None = None
    sim: SimData | None = None  # what the lap-time model takes from the recording


def _slice(sig: Signals, m: np.ndarray) -> Signals:
    return Signals(**{k: v[m] for k, v in vars(sig).items()})


def dam_log(s: Session) -> motec.MotecLog | None:
    """The DAM log of this outing, from <LMU>/LOG next to UserData, if there is one."""
    root = Path(s.path).resolve().parents[2] if len(Path(s.path).resolve().parents) > 2 else None
    log_dir = root / "LOG" if root else None
    if not log_dir or not log_dir.is_dir():
        return None
    around = None
    try:
        utc = datetime.strptime(s.metadata.get("RecordingTime", ""), "%Y-%m-%dT%H_%M_%SZ").replace(tzinfo=timezone.utc)
        around = utc.astimezone().replace(tzinfo=None)  # DAM names its logs in local time
    except ValueError:
        pass
    return motec.find_log(log_dir, s.track, s.t_start, s.t_end, around)


def observe_recording(s: Session, sig: Signals, laps: list[Lap], setup: CarSetup,
                      log: motec.MotecLog | None = None, use_dam: bool = True,
                      clean: set[int] | None = None) -> Observations:
    """``clean``: seqs of the laps setups are compared on (analysis's rule); complete laps
    outside the pits when not given."""
    if not laps:
        return Observations()
    if clean is None:
        clean = {i for i, lap in enumerate(laps) if lap.complete and not lap.in_pits}
    k = load_knowledge(s.car_class)
    geometry = steering_geometry(setup, s.car_class)
    wheelbase = geometry[2]
    cmap = corners.corner_map(sig, laps)
    out = Observations(corners=cmap)
    per = balance.sample_metrics(sig, geometry)

    if log is None and use_dam:
        try:
            log = dam_log(s)
        except (OSError, ValueError):
            log = None
    slip_fr, tyre_load_native = None, None
    if log is not None:
        offset = motec.clock_offset(log, sig.t, sig.speed * 3.6)
        dam = motec.resample(log, sig.t, offset)
        if "lat_ground_vel" in dam and "long_ground_vel" in dam:
            alpha = np.degrees(np.arctan2(dam["lat_ground_vel"], np.maximum(np.abs(dam["long_ground_vel"]), 1.0)))
            slip_fr = np.abs(alpha[:, :2]).mean(1) - np.abs(alpha[:, 2:]).mean(1)
        if "Susp Pos" in s.frequencies:
            t_native, _ = s.channel("Susp Pos")
            tyre_load_native = motec.resample(log, t_native, offset).get("tyre_load")
        out.dam = log.path.name

    susp = suspension.load_suspension(s, sig.t, sig.speed, tyre_load_native)
    kerb_grid = np.zeros(len(sig.t), dtype=bool)
    yaw_error = np.where(np.isfinite(per["r_ref"]), per["r_ref"] - sig.yaw_rate, np.nan)
    if susp is not None:
        mask, out.kerb_source = kerbs.kerb_mask(susp, sig)
        kerb_grid = kerbs.grid_mask(susp, mask, sig.t)
        out.load_source = susp.load_source
        susp.kerb = mask  # the damper split uses the same samples as the events
        evs = kerbs.events(susp, sig, yaw_error, wheelbase)
    else:
        evs = []

    band_split = float(k["band_split_kmh"]) / 3.6
    knee = float(k["damper_knee_mm_s"])
    phase_grid = np.full(len(sig.t), "", dtype=object)
    raw: list[tuple[int, balance.PhaseObs, float]] = []  # (seq, obs, pass mean speed)
    for seq, lap in enumerate(laps):
        if lap.in_pits:
            continue
        m = (sig.t >= lap.t_start) & (sig.t < lap.t_end)
        if m.sum() < 50:
            continue
        idx = np.flatnonzero(m)
        sl = _slice(sig, m)
        per_l = {key: v[m] for key, v in per.items()}
        for p in corners.passes(sl, cmap, band_split):
            phase_grid[idx[p.idx]] = np.where(p.phase == "", phase_grid[idx[p.idx]], p.phase)
            sf = slip_fr[m] if slip_fr is not None else None
            for o in balance.observe(sl, per_l, p, kerb_grid[m], sf):
                raw.append((seq, o, float(np.mean(sl.speed[p.idx]))))
    usual: dict[int, float] = {}
    for c in {o.corner for _, o, _ in raw}:
        usual[c] = float(np.median([v for _, o, v in raw if o.corner == c]))
    out.passes = [(seq, o) for seq, o, v in raw if v >= SLOW_PASS * usual[o.corner]]

    if susp is not None:
        i = np.clip(np.searchsorted(sig.t, susp.t), 0, len(sig.t) - 1)
        phase_native = phase_grid[i].astype(str)
        for seq, lap in enumerate(laps):
            if lap.in_pits:
                continue
            out.suspension += suspension.lap_suspension(susp, seq, lap.t_start, lap.t_end, phase_native, knee)
        for ev in evs:
            seq = next((n for n, lap in enumerate(laps) if lap.t_start <= ev.t0 < lap.t_end), None)
            if seq is not None and not laps[seq].in_pits:
                out.kerbs.append((seq, ev))
    out.sim = sim_extract(s, sig, laps, clean)
    return out
