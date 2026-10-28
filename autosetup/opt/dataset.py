"""Observations for the optimiser: one row per qualifying stint.

Pace is corrected before the model sees it, in two parts:
  * fuel: a per-class seconds-per-litre coefficient (classes.toml) moves every lap to a
    reference fuel load;
  * within-stint trend: tyre wear, settling into the stint and track evolution all move
    together with lap number inside a stint (and can't be told apart there), so a single
    slope per lap is fitted on stint-demeaned laps and each stint is moved to a reference
    stint lap.
What changes *between* stints and isn't the setup — you getting faster on the track,
track temperature — goes into the model as context inputs (``experience``,
``track_temp``), so similar setups driven at different times separate the two.

Transfer between tracks: stints of the same car model (signature) at other layouts come
along as ``extra`` observations. Each layout keeps its own corrections and experience
scale, and its objectives are shifted so its mean matches this layout's (a track's lap
time says nothing about the setup); the GP then relates layouts through a cross-track
correlation (see gp.py).

Objectives are costs (lower is better):
  pace         corrected mean clean lap, s
  tyres        mean wear per racing lap, % (average of the four corners)
  drivability  spread of corrected clean laps (s) + 0.1 s per countersteer moment per lap
               beyond what you're fine with (learned, opt/balance.py)
               + 0.1 s per point your rating sits below 3 (above 3 lowers it)

Balance is measured too: the understeer angle per zone, each corner phase within each corner
speed (mean of the clean laps' medians), which the optimiser models and holds against the
angle you like (balance.py).
It's measured every stint, rated or not, and is far less noisy than lap time.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from autosetup.lmu.setup_space import CarSetup
from autosetup.store import Store

MIN_CLEAN_LAPS = 3
REF_STINT_LAP = 3  # pace is reported as if every lap were the stint's 3rd racing lap
OBJECTIVES = ("pace", "tyres", "drivability")
PHASES = ("entry", "mid", "exit")
SPEEDS = ("slow", "medium", "fast")  # corner speed bands (metrics/compute.py)
# Where balance is measured and liked: each corner phase within each corner speed,
# "slow_exit" = exits of low-speed corners.
ZONES = tuple(f"{sp}_{ph}" for sp in SPEEDS for ph in PHASES)


def zone_parts(zone: str) -> tuple[str, str]:
    """'slow_exit' → ('slow', 'exit')."""
    sp, ph = zone.split("_", 1)
    return sp, ph
UNITS = {"pace": "s", "tyres": "%/lap", "drivability": "s"}

# Settings the optimiser may move. Tyres, brake cooling and front ride height stay with
# the rules (they're corrections with a clear target); fuel/energy/compound aren't setup.
# Rear ride height is rake, a balance tool: the model learns it (the rules still raise a
# rear that touches down).
OPT_BASES = [
    "WM_RIDEHEIGHT",
    # Brake pressure isn't here: it's a feel setting, raised by the rules until you flag
    # lock-ups (advisor._brake_pressure).
    "VM_FRONT_ANTISWAY", "VM_REAR_ANTISWAY", "VM_BRAKE_BALANCE",
    "VM_DIFF_PRELOAD", "VM_DIFF_POWER", "VM_DIFF_COAST", "VM_DIFF_PUMP",
    "VM_REAR_WING", "VM_FRONT_WING",
    "VM_TRACTIONCONTROLMAP", "VM_TRACTIONCONTROLPOWERCUTMAP", "VM_TRACTIONCONTROLSLIPANGLEMAP",
    "VM_ANTILOCKBRAKESYSTEMMAP", "VM_ENGINE_BRAKEMAP",
    "VM_FRONT_TOEIN", "VM_REAR_TOEIN", "VM_CASTER", "VM_STEER_LOCK",
    "WM_CAMBER", "WM_SPRING", "WM_SLOWBUMP", "WM_SLOWREBOUND",
    "WM_FASTBUMP", "WM_FASTREBOUND",
    # Third (heave) spring, its packers and dampers, on cars that have them. The rules only
    # add packer for bottoming; otherwise the model learns what suits you.
    *(f"VM_{axle}_3RD_{part}" for axle in ("FRONT", "REAR")
      for part in ("SPRING", "PACKERS", "SLOWBUMP", "SLOWREBOUND", "FASTBUMP", "FASTREBOUND")),
]
AXLES = {"F": ("FL", "FR"), "R": ("RL", "RR")}
REAR_ONLY = {"WM_RIDEHEIGHT"}


@dataclass
class Dim:
    """One optimiser input. Per-wheel settings are tied per axle (both sides move)."""

    name: str  # e.g. 'VM_REAR_ANTISWAY' or 'WM_SPRING:F'
    keys: list[str]
    lo: int
    hi: int  # inclusive

    def x(self, setup: CarSetup) -> float:
        v = np.mean([setup[k].value for k in self.keys])
        return float((v - self.lo) / max(self.hi - self.lo, 1))

    def index(self, x: float) -> int:
        return int(round(self.lo + x * (self.hi - self.lo)))


@dataclass
class Observation:
    stint_id: int
    file_name: str
    setup: CarSetup
    y: dict[str, float]
    noise: dict[str, float]  # variance of y
    experience: float  # racing laps on this car + layout before the stint
    track_temp: float | None
    clean_laps: int
    group: str = ""  # track layout; differs from the target's for transferred stints
    balance: dict[str, tuple[float, float]] = field(default_factory=dict)  # phase: (angle, variance)


@dataclass
class Dataset:
    domain: str  # car_signature|layout
    dims: list[Dim]
    obs: list[Observation]
    fuel_s_per_l: float
    trend_s_per_lap: float
    lap_sigma: float  # pooled lap-time noise after corrections, s
    excluded: list[str] = field(default_factory=list)
    ref_fuel: float = 0.0  # litres every lap is corrected to
    extra: list[Observation] = field(default_factory=list)  # same car model at other layouts

    @property
    def target(self) -> str:
        return self.domain.split("|", 1)[1] if "|" in self.domain else ""

    @property
    def transfer_from(self) -> list[str]:
        return sorted({o.group for o in self.extra})

    @property
    def all_obs(self) -> list[Observation]:
        return self.obs + self.extra

    def groups(self) -> np.ndarray:
        """0 for this layout, 1.. for each other layout, per row of ``X_all``."""
        codes = {g: i + 1 for i, g in enumerate(self.transfer_from)}
        return np.array([0] * len(self.obs) + [codes[o.group] for o in self.extra], dtype=int)

    def X(self, setups: list[CarSetup] | None = None, context: list[tuple[float, float]] | None = None) -> np.ndarray:
        """Inputs at this layout: one column per dim in [0, 1], then experience and track
        temperature scaled to the observed range."""
        setups = setups if setups is not None else [o.setup for o in self.obs]
        ctx = context if context is not None else [(o.experience, o.track_temp) for o in self.obs]
        rows = [[d.x(s) for d in self.dims] + list(self.scale_context(*c)) for s, c in zip(setups, ctx)]
        return np.array(rows, dtype=float).reshape(len(rows), len(self.dims) + 2)

    def X_all(self) -> np.ndarray:
        """Inputs of every observation, transferred ones scaled within their own layout."""
        rows = [[d.x(o.setup) for d in self.dims] + list(self.scale_context(o.experience, o.track_temp, o.group))
                for o in self.all_obs]
        return np.array(rows, dtype=float).reshape(len(rows), len(self.dims) + 2)

    def scale_context(self, experience: float, track_temp: float | None, group: str | None = None) -> tuple[float, float]:
        """Experience is scaled within a layout (each has its own learning curve); track
        temperature over everything."""
        mine = self.obs if group is None or group == self.target else [o for o in self.extra if o.group == group]
        exps = [np.log1p(o.experience) for o in (mine or self.all_obs)]
        temps = [o.track_temp for o in self.all_obs if o.track_temp is not None]
        e0, e1 = (min(exps), max(exps)) if exps else (0.0, 1.0)
        t0, t1 = (min(temps), max(temps)) if temps else (0.0, 1.0)
        e = (np.log1p(experience) - e0) / (e1 - e0) if e1 > e0 else 0.0
        t = ((track_temp if track_temp is not None else (t0 + t1) / 2) - t0) / (t1 - t0) if t1 > t0 else 0.0
        return float(e), float(t)

    def Y(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        return (np.array([o.y[name] for o in self.obs], dtype=float),
                np.array([o.noise[name] for o in self.obs], dtype=float))

    def Y_all(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        """Every observation; each other layout shifted so its mean matches this one's."""
        y, noise = self.Y(name)
        if not self.extra:
            return y, noise
        ref = float(np.mean(y)) if len(y) else float(np.mean([o.y[name] for o in self.extra]))
        ys, ns = list(y), list(noise)
        for g in self.transfer_from:
            mine = [o for o in self.extra if o.group == g]
            mean = float(np.mean([o.y[name] for o in mine]))
            ys += [o.y[name] - mean + ref for o in mine]
            ns += [o.noise[name] for o in mine]
        return np.array(ys, dtype=float), np.array(ns, dtype=float)

    def B_all(self, phase: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(rows of ``X_all`` that measured this phase, angle, noise), each other layout
        shifted so its mean matches this one's (corners differ between tracks)."""
        rows = [i for i, o in enumerate(self.all_obs) if phase in o.balance]
        y = np.array([self.all_obs[i].balance[phase][0] for i in rows], dtype=float)
        noise = np.array([self.all_obs[i].balance[phase][1] for i in rows], dtype=float)
        n_own = sum(1 for i in rows if i < len(self.obs))
        if self.extra and len(rows) > n_own:
            groups = self.groups()[rows]
            ref = float(np.mean(y[:n_own])) if n_own else float(np.mean(y))
            for g in set(groups[n_own:].tolist()):
                m = groups == g
                y[m] += ref - float(np.mean(y[m]))
        return np.array(rows, dtype=int), y, noise

    @property
    def distinct_setups(self) -> int:
        return len({tuple(d.index(d.x(o.setup)) for d in self.dims) for o in self.obs})

    @property
    def distinct_setups_all(self) -> int:
        return len({tuple(d.index(d.x(o.setup)) for d in self.dims) for o in self.all_obs})


def domain_of(store: Store, stint_id: int) -> str:
    row = store.con.execute(
        "select car_signature, coalesce(track_layout, track) from recordings where id = ?", [stint_id]).fetchone()
    if row is None:
        raise KeyError(stint_id)
    return f"{row[0]}|{row[1]}"


def build_dims(setup: CarSetup) -> list[Dim]:
    dims = []
    for base in OPT_BASES:
        if base.startswith("WM_"):
            for axle, wheels in AXLES.items():
                if base in REAR_ONLY and axle != "R":
                    continue
                keys = [f"{base}-W_{w}" for w in wheels]
                ps = [setup.params.get(k) for k in keys]
                if all(p is not None and p.adjustable for p in ps):
                    dims.append(Dim(f"{base}:{axle}", keys, ps[0].min, ps[0].max - 1))
        elif base == "VM_CASTER":
            keys = ["VM_LEFT_CASTER", "VM_RIGHT_CASTER"]
            ps = [setup.params.get(k) for k in keys]
            if all(p is not None and p.adjustable for p in ps):
                dims.append(Dim("VM_CASTER", keys, ps[0].min, ps[0].max - 1))
        else:
            p = setup.params.get(base)
            if p is not None and p.adjustable:
                dims.append(Dim(base, [base], p.min, p.max - 1))
    return dims


def build_dataset(store: Store, domain: str, fuel_s_per_l: float, rating_weight: float = 0.1,
                  transfer: bool = True, tolerance: Callable[[str], dict[str, float]] | None = None) -> Dataset:
    """Observations of one car model at one layout. With ``transfer``, the same car
    model's stints at other layouts come along as ``extra``. ``tolerance``: per layout,
    the countersteer per lap and phase you're fine with (Balance.tolerance); only
    countersteer beyond it makes a stint harder to drive. Without it, all of it does."""
    signature, layout = domain.split("|", 1)
    obs, excluded, trend, sigma, ref_fuel, setups = _observations(store, signature, layout, fuel_s_per_l, rating_weight,
                                                                  tolerance)
    extra: list[Observation] = []
    if transfer:
        others = store.con.execute(
            """select distinct coalesce(track_layout, track) from recordings
               where car_signature = ? and coalesce(track_layout, track) <> ? and duration > 30""",
            [signature, layout]).fetchall()
        for (other,) in sorted(others):
            extra += [o for o in _observations(store, signature, other, fuel_s_per_l, rating_weight, tolerance)[0]
                      if not any(np.isnan(v) for v in o.y.values())]
    base_setup = obs[-1].setup if obs else (setups[-1] if setups else (extra[-1].setup if extra else None))
    dims = build_dims(base_setup) if base_setup else []
    return Dataset(domain, dims, obs, fuel_s_per_l, trend, sigma, excluded, ref_fuel, extra)


def _observations(store: Store, signature: str, layout: str, fuel_s_per_l: float, rating_weight: float,
                  tolerance: Callable[[str], dict[str, float]] | None = None
                  ) -> tuple[list[Observation], list[str], float, float, float, list[CarSetup]]:
    """(observations, stints left out, trend, lap sigma, reference fuel, every setup in
    order) of one car model at one layout."""
    tol = tolerance(layout) if tolerance else None
    recs = store.con.execute(
        """select id, file_name, setup_json from recordings
           where car_signature = ? and coalesce(track_layout, track) = ? and duration > 30
           order by file_mtime""", [signature, layout]).fetchall()
    laps = _lap_table(store, [r[0] for r in recs])
    trend, sigma = _within_stint_trend(laps, fuel_s_per_l)
    fuels = [l["fuel"] for l in laps if l["clean"] and l["fuel"] is not None]
    ref_fuel = float(np.mean(fuels)) if fuels else 0.0
    experience = 0.0
    obs: list[Observation] = []
    excluded: list[str] = []
    setups = []
    for rid, file_name, setup_json in recs:
        setup = CarSetup.from_json(json.loads(setup_json))
        setups.append(setup)
        rows = [l for l in laps if l["rid"] == rid]
        racing = [l for l in rows if l["racing"]]
        clean = [l for l in rows if l["clean"]]
        exp_before = experience
        experience += len(racing)
        if len(clean) < MIN_CLEAN_LAPS:
            excluded.append(f"{file_name}: {len(clean)} clean lap{'s' if len(clean) != 1 else ''}")
            continue
        corrected = np.array([_correct(l, fuel_s_per_l, trend, ref_fuel) for l in clean])
        wear = [l["wear"] for l in racing if l["wear"] is not None]
        if tol is None:
            cs = [l["countersteer"] for l in racing if l["countersteer"] is not None]
        else:
            cs = [sum(max(0.0, l[f"cs_{ph}"] - tol[ph]) for ph in PHASES if l[f"cs_{ph}"] is not None) for l in racing]
        rating = store.rating(file_name) or {}
        spread = float(np.std(corrected, ddof=1))
        drive = spread + 0.1 * (float(np.mean(cs)) if cs else 0.0)
        if rating.get("rating") is not None:
            drive += rating_weight * (3 - rating["rating"])
        balance = {}
        for ph in ZONES:
            us = [l[f"us_{ph}"] for l in clean if l[f"us_{ph}"] is not None]
            if us:
                var = float(np.var(us, ddof=1)) / len(us) if len(us) > 1 else 0.1 ** 2
                balance[ph] = (float(np.mean(us)), var + 0.02 ** 2)
        n = len(clean)
        y = {"pace": float(np.mean(corrected)), "tyres": float(np.mean(wear)) if wear else np.nan, "drivability": drive}
        noise = {
            "pace": max(sigma, spread) ** 2 / n,
            "tyres": (float(np.var(wear, ddof=1)) / len(wear) if len(wear) > 1 else 0.01) + 1e-4,
            "drivability": spread ** 2 / (2 * (n - 1)) + 0.02 ** 2,
        }
        temps = [l["track_temp"] for l in racing if l["track_temp"] is not None]
        obs.append(Observation(rid, file_name, setup, y, noise, exp_before,
                               float(np.mean(temps)) if temps else None, n, layout, balance))
    obs = [o for o in obs if not np.isnan(o.y["tyres"])] if any(not np.isnan(o.y["tyres"]) for o in obs) else obs
    return obs, excluded, trend, sigma, ref_fuel, setups


def _lap_table(store: Store, rids: list[int]) -> list[dict]:
    """Every complete lap of these recordings with what the corrections need."""
    if not rids:
        return []
    from autosetup.analysis import laps_of  # clean-lap rule lives there

    wanted = ["fuel_start", "track_temp", "countersteer_events", "wear_fl", "wear_fr", "wear_rl", "wear_rr",
              *(f"us_{ph}" for ph in ZONES), *(f"cs_{ph}" for ph in PHASES)]
    metric_rows = store.con.execute(
        "select recording_id, seq, name, value from lap_metrics where recording_id in (select unnest(?::INTEGER[])) "
        "and name in (select unnest(?::VARCHAR[]))", [rids, wanted]).fetchall()
    m: dict[tuple[int, int], dict[str, float]] = {}
    for rid, seq, name, value in metric_rows:
        m.setdefault((rid, seq), {})[name] = value
    out = []
    laps_by = laps_of(store, rids)
    for rid in rids:
        racing_no = 0
        for lap in laps_by[rid]:
            if not lap["complete"] or lap["in_pits"]:
                continue
            racing_no += 1
            mm = m.get((rid, lap["seq"]), {})
            wear = [mm.get(f"wear_{w}") for w in ("fl", "fr", "rl", "rr")]
            out.append(dict(
                rid=rid, seq=lap["seq"], time=lap["time"], clean=lap["clean"], racing=True, stint_lap=racing_no,
                fuel=mm.get("fuel_start"), track_temp=mm.get("track_temp"),
                countersteer=mm.get("countersteer_events"),
                **{f"us_{ph}": mm.get(f"us_{ph}") for ph in ZONES},
                **{f"cs_{ph}": mm.get(f"cs_{ph}") for ph in PHASES},
                wear=float(np.mean(wear)) if all(v is not None for v in wear) else None,
            ))
    return out


def _within_stint_trend(laps: list[dict], fuel_s_per_l: float) -> tuple[float, float]:
    """Slope of fuel-corrected clean lap time per stint lap, from stint-demeaned laps
    (fixed effects), shrunk towards 0; and the pooled residual spread."""
    groups: dict[int, list[tuple[float, float]]] = {}
    for l in laps:
        if l["clean"]:
            t = l["time"] - fuel_s_per_l * (l["fuel"] or 0.0)
            groups.setdefault(l["rid"], []).append((float(l["stint_lap"]), t))
    xs, ys = [], []
    for pts in groups.values():
        if len(pts) < 3:
            continue
        a = np.array(pts)
        xs.append(a[:, 0] - a[:, 0].mean())
        ys.append(a[:, 1] - a[:, 1].mean())
    if not xs:
        return 0.0, 0.3
    x, y = np.concatenate(xs), np.concatenate(ys)
    ridge = 20.0  # ≈ 20 laps' worth of evidence before trusting a slope
    slope = float((x @ y) / (x @ x + ridge))
    resid = y - slope * x
    dof = max(len(y) - len(xs) - 1, 1)
    return slope, float(np.sqrt(resid @ resid / dof))


def _correct(lap: dict, fuel_s_per_l: float, trend: float, ref_fuel: float) -> float:
    """The lap as if driven with ``ref_fuel`` litres on board, on the stint's
    REF_STINT_LAP-th racing lap."""
    fuel = lap["fuel"] if lap["fuel"] is not None else ref_fuel
    return lap["time"] - fuel_s_per_l * (fuel - ref_fuel) - trend * (lap["stint_lap"] - REF_STINT_LAP)
