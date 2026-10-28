"""The calibrated lap model of one car at one layout, and the car's aero map, from the store.

Only the current game build's recordings are used (lmu/builds.py).

  LayoutModel  the layout's track (from your fastest lap's profile), the car pooled over
               its stints there, each stint's drag and downforce against that pool, and the
               driver efficiency η that makes the simulated laps match yours
  aero_map     the car's aero map over every layout it has stints at, kept in
               ``aero_maps`` per car model and build
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from autosetup.lmu.builds import current_build, current_filter
from autosetup.lmu.setup_space import CarSetup
from autosetup.metrics.balance import PhaseObs, cells, normalise
from autosetup.sim.aero import AeroMap, Platform, Stint, drag_deltas, layout_car, lift_deltas, shrink
from autosetup.sim.extract import STEP_M, straights_of
from autosetup.sim.lapsim import Car, Result, Track, fit_eta, simulate

SCHEMA = """
create table if not exists aero_maps (
    car_signature varchar,
    game_build    varchar,
    data          json,
    stints        integer,
    updated_at    timestamp,
    primary key (car_signature, game_build)
);
"""
LIFT_SHRINK = 0.3  # prior sd of one stint's downforce against the pool, as a share of the class's c


@dataclass
class StintData:
    rid: int
    file_name: str
    setup: CarSetup
    platform: Platform | None
    envelope: list[tuple[str, float, int, float]]
    straights: list[tuple[int, int, int, float, float]]  # (rid, bin, n, v, a_x)
    traps: list[tuple[int, float, float]]  # (seq, d, v)
    profile: tuple[np.ndarray, np.ndarray, float, list[float]] | None  # κ, v, lap time, sectors
    clean: list[int] = field(default_factory=list)  # clean lap seqs


def wing_share(setup: CarSetup) -> float:
    p = setup.params.get("VM_REAR_WING")
    return p.fraction if p is not None and p.adjustable else 0.0


def stints_of(store, signature: str, layout: str | None = None) -> list[StintData]:
    """The current build's stints of a car model (at one layout, or all)."""
    from autosetup.analysis import laps_of

    cond, params = current_filter(store)
    sql = (f"select r.id, r.file_name, r.setup_json, r.rh_front_fast, r.rh_rear_fast, coalesce(r.track_layout, r.track) "
           f"from recordings r where r.car_signature = ? and {cond}")
    args = [signature, *params]
    if layout is not None:
        sql += " and coalesce(r.track_layout, r.track) = ?"
        args.append(layout)
    rows = store.con.execute(sql + " order by r.file_mtime", args).fetchall()
    rids = [r[0] for r in rows]
    if not rids:
        return []
    env, st, tr, prof = {}, {}, {}, {}
    for rid, kind, v, n, value in store.con.execute(
            "select recording_id, kind, v, n, value from sim_envelope where recording_id in (select unnest(?::INTEGER[]))",
            [rids]).fetchall():
        env.setdefault(rid, []).append((kind, v, n, value))
    for rid, seq, b, n, v, a in store.con.execute(
            "select recording_id, seq, bin, n, v, a_x from sim_straights where recording_id in (select unnest(?::INTEGER[]))",
            [rids]).fetchall():
        st.setdefault(rid, []).append((rid, b, n, v, a))
    for rid, seq, d, v in store.con.execute(
            "select recording_id, seq, d, v from sim_traps where recording_id in (select unnest(?::INTEGER[]))",
            [rids]).fetchall():
        tr.setdefault(rid, []).append((seq, d, v))
    for rid, lap_time, kappa, speed, sectors in store.con.execute(
            "select recording_id, lap_time, kappa, speed, sectors from sim_profiles "
            "where recording_id in (select unnest(?::INTEGER[]))", [rids]).fetchall():
        prof[rid] = (np.array(json.loads(kappa)), np.array(json.loads(speed)), float(lap_time), json.loads(sectors))
    laps = laps_of(store, rids)
    out = []
    for rid, name, sj, rhf, rhr, _ in rows:
        setup = CarSetup.from_json(json.loads(sj))
        platform = Platform(wing_share(setup), rhf, rhr) if rhf is not None and rhr is not None else None
        out.append(StintData(rid, name, setup, platform, env.get(rid, []), st.get(rid, []), tr.get(rid, []),
                             prof.get(rid), [lap["seq"] for lap in laps[rid] if lap["clean"]]))
    return out


@dataclass
class LayoutModel:
    signature: str
    layout: str
    track: Track
    base: Car  # pooled over the stints, at their mean platform
    eta: float
    platform: Platform | None  # the stints' mean platform (what ``base`` stands for)
    drag: dict[int, tuple[float, float]]  # per stint Δk, se
    lift: dict[int, float]  # per stint Δc (shrunk)
    stints: list[StintData]

    @classmethod
    def build(cls, store, signature: str, layout: str, knowledge: dict) -> "LayoutModel | None":
        stints = [s for s in stints_of(store, signature, layout) if s.envelope]
        with_profile = [s for s in stints if s.profile is not None]
        if not with_profile:
            return None
        lift_typ = float(knowledge["lift_c_typ"])
        base = layout_car([s.envelope for s in stints], lift_typ, float(knowledge["drag_k_typ"]))
        if base is None:
            return None
        kappa, _, _, sectors = min(with_profile, key=lambda s: s.profile[2]).profile
        track = Track(STEP_M, kappa, tuple(sectors), tuple(d1 for _, d1 in straights_of(kappa)))
        drag = drag_deltas([row for s in stints for row in s.straights])
        lift = shrink(lift_deltas({s.rid: s.envelope for s in stints}, base.mu), LIFT_SHRINK * lift_typ)
        platforms = [s.platform for s in stints if s.platform is not None]
        mean = (Platform(float(np.mean([p.wing for p in platforms])), float(np.mean([p.rh_front for p in platforms])),
                         float(np.mean([p.rh_rear for p in platforms]))) if platforms else None)
        model = cls(signature, layout, track, base, 1.0, mean, drag, lift, stints)
        model.eta = fit_eta([(Track(STEP_M, s.profile[0], tuple(s.profile[3])), model.stint_car(s.rid), s.profile[2])
                             for s in with_profile])
        return model

    def stint_car(self, rid: int) -> Car:
        """The pooled car moved by what this stint measured, at the fitted η."""
        car = self.base.perturb(d_drag=self.drag.get(rid, (0.0, 0.0))[0], d_lift=self.lift.get(rid, 0.0))
        return car.scaled(self.eta)

    def simulate(self, car: Car) -> Result:
        """``car`` as stint_car / car_at return it (η applied)."""
        return simulate(self.track, car)

    def car_at(self, aero: AeroMap, platform: Platform) -> Car:
        """The car on another aero platform, through the aero map."""
        if self.platform is None:
            return self.base.scaled(self.eta)
        return aero.car(self.base, self.platform, platform).scaled(self.eta)


def stint_balance(store, rid: int) -> tuple[float, float] | None:
    """High- minus low-speed understeer (deg, se), pooled over the phases: the aero balance
    shows here."""
    cols = [d[0] for d in store.con.execute("select * from corner_passes limit 0").description]
    rows = store.con.execute("select * from corner_passes where recording_id = ?", [rid]).fetchall()
    obs = [PhaseObs(**{k: v for k, v in zip(cols, r) if k not in ("recording_id", "seq")}) for r in rows]
    c = cells(normalise(obs))
    diffs = [(c[(ph, "high")].mean - c[(ph, "low")].mean, c[(ph, "high")].se ** 2 + c[(ph, "low")].se ** 2)
             for ph in ("entry", "mid", "exit") if (ph, "high") in c and (ph, "low") in c]
    if not diffs:
        return None
    w = np.array([1 / v for _, v in diffs])
    return float(np.sum(w * np.array([d for d, _ in diffs])) / w.sum()), float(np.sqrt(1 / w.sum()))


def aero_map(store, signature: str, knowledge: dict, refit: bool = False) -> AeroMap:
    """The car's aero map for the current build, fitted over all its layouts (and kept)."""
    store.con.execute(SCHEMA)
    build = current_build(store) or ""
    stints = [s for s in stints_of(store, signature) if s.platform is not None]
    if not refit:
        row = store.con.execute("select data, stints from aero_maps where car_signature = ? and game_build = ?",
                                [signature, build]).fetchone()
        if row and row[1] == len(stints):
            return AeroMap.from_json(json.loads(row[0]))
    layouts = dict(store.con.execute(
        "select id, coalesce(track_layout, track) from recordings where id in (select unnest(?::INTEGER[]))",
        [[s.rid for s in stints] or [-1]]).fetchall())
    evidence = []
    for layout in sorted(set(layouts.values())):
        mine = [s for s in stints if layouts[s.rid] == layout]
        drag = drag_deltas([row for s in mine for row in s.straights])
        base = layout_car([s.envelope for s in mine if s.envelope], float(knowledge["lift_c_typ"]),
                          float(knowledge["drag_k_typ"]))
        lift = lift_deltas({s.rid: s.envelope for s in mine}, base.mu) if base is not None else {}
        for s in mine:
            evidence.append(Stint(layout, s.platform, drag.get(s.rid), lift.get(s.rid), stint_balance(store, s.rid)))
    amap = AeroMap.fit(evidence, knowledge)
    store.con.execute("insert or replace into aero_maps values (?, ?, ?, ?, ?)",
                      [signature, build, json.dumps(amap.to_json()), len(stints),
                       datetime.now(timezone.utc).replace(tzinfo=None)])
    return amap


def sector_times(store, rid: int, bounds: tuple[float, ...], seqs: list[int]) -> list[list[float]]:
    """Per clean lap, the time of each sector between ``bounds`` (from its 100 m minisectors)."""
    from autosetup.metrics.compute import SECTOR_M

    out = []
    for seq in seqs:
        times = [t for (t,) in store.con.execute(
            "select time from lap_sectors where recording_id = ? and seq = ? order by sector", [rid, seq]).fetchall()]
        if not times:
            continue
        d = np.concatenate([[0.0], np.minimum(np.arange(1, len(times) + 1) * SECTOR_M, np.inf)])
        cum = np.concatenate([[0.0], np.cumsum(times)])
        marks = [float(np.interp(b, d, cum)) for b in bounds]
        edges = [0.0, *marks, float(cum[-1])]
        out.append([b - a for a, b in zip(edges, edges[1:])])
    return out
