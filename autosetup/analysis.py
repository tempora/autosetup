"""Stint-level views over the store: what the dashboard and later the optimiser read.

A stint is one recording (the game starts a new file each time you leave the garage).
Stints are grouped by *context*: the livery-level car name plus the track layout, since
Sebring School and the full Sebring share a track name but not a setup.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

import numpy as np

from autosetup.lmu.setup_space import CarSetup
from autosetup.store import Store

CLEAN_SLOW_FACTOR = 1.03  # laps slower than this × the stint median are traffic/mistakes
WHEELS = ("fl", "fr", "rl", "rr")

# Lap metrics averaged into the stint summary (per-wheel ones are expanded below).
SUMMARY_METRICS = [
    "us_entry", "us_mid", "us_exit", "us_slow", "us_fast", "countersteer_events", "cs_entry", "cs_mid", "cs_exit",
    "steer_reversals_per_min", "tc_s", "abs_s", "lat_g_p98", "brake_g_p98", "bottoming_s",
    "rake_fast", "full_throttle_frac", "coast_frac", "trail_brake_frac", "throttle_apply_rate",
    "track_temp", "ambient_temp", "fuel_used", "kerb_s", "overheating_s", "speed_max", "speed_min",
    "third_travel_f", "third_travel_r", "third_packers_f", "third_packers_r", "third_contact_f", "third_contact_r",
]
PER_WHEEL = ["t_inner", "t_centre", "t_outer", "t_spread", "t_crown", "p_hot", "t_carcass", "wear",
             "brake_temp_max", "rh_min"]


@dataclass
class Context:
    car_name: str
    car_class: str
    track: str
    layout: str
    stints: int
    last_recorded: str

    @property
    def key(self) -> str:
        return f"{self.car_name}|{self.layout}"


@dataclass
class Stint:
    id: int
    file_name: str
    recorded_at: str
    session_type: str
    setup_name: str | None  # saved .svm it matches, if any
    laps: list[dict]
    summary: dict[str, float | None]
    rating: dict | None = None
    changes: list[dict] = field(default_factory=list)  # setup diff vs previous stint
    pareto: bool = False

    def to_json(self) -> dict:
        return asdict(self)


def contexts(store: Store) -> list[Context]:
    rows = store.con.execute(
        """select car_name, any_value(car_class), any_value(track), coalesce(track_layout, track) as layout,
                  count(*), max(recorded_at)
           from recordings where duration > 30
           group by car_name, layout order by max(file_mtime) desc"""
    ).fetchall()
    return [Context(*r) for r in rows]


def stints(store: Store, car_name: str, layout: str, min_clean: int = 0) -> list[Stint]:
    recs = store.con.execute(
        """select r.id, r.file_name, r.recorded_at, r.session_type, r.setup_json, s.svm_path
           from recordings r left join recording_setups s using (file_name)
           where r.car_name = ? and coalesce(r.track_layout, r.track) = ? and r.duration > 30
           order by r.file_mtime""",
        [car_name, layout],
    ).fetchall()
    out: list[Stint] = []
    prev_setup: CarSetup | None = None
    rids = [r[0] for r in recs]
    laps_by = laps_of(store, rids)
    summary_by = summaries(store, rids, laps_by)
    for rid, file_name, recorded_at, session_type, setup_json, svm_path in recs:
        setup = CarSetup.from_json(json.loads(setup_json))
        st = Stint(
            id=rid, file_name=file_name, recorded_at=recorded_at, session_type=session_type,
            setup_name=re.sub(r"\.svm$", "", svm_path.replace("\\", "/").rsplit("/", 1)[-1]) if svm_path else None,
            laps=laps_by[rid], summary=summary_by[rid], rating=store.rating(file_name),
            changes=setup_diff(prev_setup, setup) if prev_setup else [],
        )
        prev_setup = setup
        if st.summary["clean_laps"] >= min_clean:
            out.append(st)
    _mark_pareto(out)
    return out


def stint_summary(store: Store, rid: int) -> dict[str, float | None]:
    """Summary of one recording, the same numbers ``stints()`` reports for it."""
    return summaries(store, [rid])[rid]


def summaries(store: Store, rids: list[int], laps: dict[int, list[dict]] | None = None) -> dict[int, dict]:
    """``stint_summary`` of many recordings in three queries, not three per recording
    (with hundreds of stints the per-recording round trips were most of the time)."""
    laps = laps if laps is not None else laps_of(store, rids)
    names = SUMMARY_METRICS + [f"{n}_{w}" for n in PER_WHEEL for w in WHEELS]
    means: dict[int, dict[str, float]] = {rid: {} for rid in rids}
    if rids:
        # Tyre/platform/handling metrics average over every racing lap (complete, not in
        # pits); a lap with a moment off the track still loads the tyres normally.
        for rid, name, value in store.con.execute(
                """select m.recording_id, m.name, avg(m.value) from lap_metrics m
                   join laps l on l.recording_id = m.recording_id and l.seq = m.seq and l.complete and not l.in_pits
                   where m.recording_id in (select unnest(?::INTEGER[])) and m.name in (select unnest(?::VARCHAR[]))
                   group by all""", [rids, names]).fetchall():
            means[rid][name] = value
    sectors: dict[int, list[tuple[int, int, float]]] = {rid: [] for rid in rids}
    if rids:
        for rid, seq, sector, t in store.con.execute(
                "select recording_id, seq, sector, time from lap_sectors where recording_id in (select unnest(?::INTEGER[]))",
                [rids]).fetchall():
            sectors[rid].append((seq, sector, t))
    return {rid: _summarise(laps[rid], means[rid], sectors[rid], names) for rid in rids}


def laps_of(store: Store, rids: list[int]) -> dict[int, list[dict]]:
    """``_laps`` of many recordings in one query."""
    out: dict[int, list[dict]] = {rid: [] for rid in rids}
    if not rids:
        return out
    rows = store.con.execute(
        """select l.recording_id, l.seq, l.lap_number, l.lap_time, l.complete, l.in_pits,
                  coalesce(m.off_track, 0) > 0, coalesce(m.off_major, m.off_track, 0) > 0
           from laps l left join (
               select recording_id, seq, max(value) filter (where name = 'off_track') as off_track,
                      max(value) filter (where name = 'off_major') as off_major
               from lap_metrics where recording_id in (select unnest(?::INTEGER[]))
                 and name in ('off_track', 'off_major') group by recording_id, seq
           ) m on m.recording_id = l.recording_id and m.seq = l.seq
           where l.recording_id in (select unnest(?::INTEGER[])) order by l.recording_id, l.seq""",
        [rids, rids],
    ).fetchall()
    for rid, *r in rows:
        out[rid].append(dict(seq=r[0], lap=r[1], time=r[2], complete=r[3], in_pits=r[4], off_track=r[5], off_major=r[6]))
    for laps in out.values():
        _mark_clean(laps)
    return out


def context_of(store: Store, rid: int) -> str:
    row = store.con.execute(
        "select car_name, coalesce(track_layout, track) from recordings where id = ?", [rid]
    ).fetchone()
    if row is None:
        raise KeyError(rid)
    return f"{row[0]}|{row[1]}"


def _laps(store: Store, rid: int) -> list[dict]:
    """Laps of a recording. ``off_track`` flags any track-limit excursion (shown in the
    Laps view); ``clean`` — the laps setups are compared on — only drops real offs and laps
    more than 3 % off the stint median, so using the kerbs and run-off doesn't throw away
    good laps."""
    return laps_of(store, [rid])[rid]


def _mark_clean(laps: list[dict]) -> None:
    racing = [lap["time"] for lap in laps if lap["complete"] and not lap["in_pits"] and not lap["off_major"]]
    limit = np.median(racing) * CLEAN_SLOW_FACTOR if racing else 0.0
    for lap in laps:
        lap["clean"] = bool(lap["complete"] and not lap["in_pits"] and not lap["off_major"] and lap["time"] <= limit)


def _summarise(laps: list[dict], means: dict[str, float], sectors: list[tuple[int, int, float]],
               names: list[str]) -> dict[str, float | None]:
    """``means``: metric averages over the racing laps; ``sectors``: (seq, sector, time)."""
    clean = [lap for lap in laps if lap["clean"]]
    times = np.array([lap["time"] for lap in clean])
    summary: dict[str, float | None] = {
        "laps": len(laps),
        "clean_laps": len(clean),
        "best": float(times.min()) if len(times) else None,
        "median": float(np.median(times)) if len(times) else None,
        # Lap-to-lap spread of clean laps: the drivability headline for now.
        "consistency": float(np.std(times, ddof=1)) if len(times) >= 3 else None,
        "theoretical_best": _theoretical_best(sectors, {lap["seq"] for lap in clean}),
    }
    summary["racing_laps"] = sum(1 for lap in laps if lap["complete"] and not lap["in_pits"])
    for n in names:
        summary[n] = means.get(n)
    wear = [summary.get(f"wear_{w}") for w in WHEELS]
    summary["wear_per_lap"] = float(np.mean(wear)) if all(v is not None for v in wear) else None
    return summary


def _theoretical_best(sectors: list[tuple[int, int, float]], clean_seqs: set[int]) -> float | None:
    """Sum of each minisector's best time over the clean laps."""
    if len(clean_seqs) < 2:
        return None
    best: dict[int, float] = {}
    for seq, sector, t in sectors:
        if seq in clean_seqs and (sector not in best or t < best[sector]):
            best[sector] = t
    return float(sum(best.values())) if best else None


# -- Pareto -----------------------------------------------------------------------------
OBJECTIVES = (("median", "Pace", "s"), ("wear_per_lap", "Tyre wear", "%/lap"), ("consistency", "Consistency", "s"))


def _mark_pareto(items: list[Stint]) -> None:
    """Flag stints no other stint beats on all three objectives (all minimised)."""
    pts = [(st, [st.summary.get(k) for k, _, _ in OBJECTIVES]) for st in items]
    pts = [(st, v) for st, v in pts if all(x is not None for x in v)]
    for st, v in pts:
        st.pareto = not any(
            all(o <= x for o, x in zip(w, v)) and any(o < x for o, x in zip(w, v)) for other, w in pts if other is not st
        )


# -- setup diffs --------------------------------------------------------------------------
_NAMES = {
    "ANTISWAY": "anti-roll bar", "TOEIN": "toe", "RIDEHEIGHT": "ride height", "SLOWBUMP": "slow bump",
    "FASTBUMP": "fast bump", "SLOWREBOUND": "slow rebound", "FASTREBOUND": "fast rebound",
    "BRAKE_BALANCE": "brake bias", "BRAKE_PRESSURE": "brake pressure", "DIFF_PRELOAD": "diff preload",
    "DIFF_POWER": "diff power", "DIFF_COAST": "diff coast", "DIFF_PUMP": "diff pump",
    "ENGINE_BRAKEMAP": "engine braking", "CASTER": "caster", "LEFT_CASTER": "left caster", "RIGHT_CASTER": "right caster",
    "TRACTIONCONTROLMAP": "TC map",
    "TRACTIONCONTROLPOWERCUTMAP": "TC cut", "TRACTIONCONTROLSLIPANGLEMAP": "TC slip",
    "ANTILOCKBRAKESYSTEMMAP": "ABS map", "REAR_WING": "rear wing", "FRONT_WING": "front wing",
    "FUEL_LEVEL": "fuel", "VIRTUAL_ENERGY": "virtual energy", "STEER_LOCK": "steering lock",
    "ENGINE_MIXTURE": "engine mix", "BRAKE_DUCTS": "front brake ducts", "BRAKE_DUCTS_REAR": "rear brake ducts",
    "WATER_RADIATOR": "radiator", "OIL_RADIATOR": "oil cooler", "PACKERS": "packers", "SPRING": "spring",
    "TENDERSPRING": "tender spring", "TENDERSPRINGTRAVEL": "tender travel", "CAMBER": "camber",
    "PRESSURE": "pressure", "COMPOUND": "compound", "BRAKEDISC": "brake disc", "BRAKEPAD": "brake pad",
}


def pretty_name(key: str) -> str:
    """'WM_CAMBER-W_FL' → 'Camber FL', 'VM_REAR_ANTISWAY' → 'Rear anti-roll bar'."""
    m = re.fullmatch(r"WM_(.+)-W_(FL|FR|RL|RR)", key)
    if m:
        return f"{_NAMES.get(m.group(1), m.group(1).lower()).capitalize()} {m.group(2)}"
    body = key.removeprefix("VM_")
    for prefix in ("FRONT_3RD_", "REAR_3RD_"):
        if body.startswith(prefix):
            rest = body.removeprefix(prefix)
            return f"{prefix[:-5].capitalize()} 3rd {_NAMES.get(rest, rest.lower())}"
    if body in _NAMES:
        return _NAMES[body][0].upper() + _NAMES[body][1:]
    for side in ("FRONT_", "REAR_"):
        if body.startswith(side) and body.removeprefix(side) in _NAMES:
            return f"{side[:-1].capitalize()} {_NAMES[body.removeprefix(side)]}"
    return body.replace("_", " ").capitalize()


def setup_diff(before: CarSetup, after: CarSetup) -> list[dict]:
    """Settings that changed, in a stable order. Left/right pairs that changed together
    collapse into one row ('Camber FL/FR') when they moved identically."""
    rows = []
    for key, p in after.params.items():
        q = before.params.get(key)
        if q is None or q.value == p.value or not p.adjustable:
            continue
        if q.text.strip() == p.text.strip() and key.startswith("WM_COMPOUND"):
            continue  # same compound, different tyre set
        rows.append(dict(key=key, name=pretty_name(key), before=q.value, after=p.value, delta=p.value - q.value,
                         before_text=q.text.strip(), after_text=p.text.strip()))
    merged, used = [], set()
    by_key = {r["key"]: r for r in rows}
    for r in rows:
        if r["key"] in used:
            continue
        m = re.fullmatch(r"(WM_.+-W_)(FL|RL)", r["key"])
        twin = by_key.get(f"{m.group(1)}{'FR' if m.group(2) == 'FL' else 'RR'}") if m else None
        if twin and (twin["before"], twin["after"]) == (r["before"], r["after"]):
            r = dict(r, name=f"{r['name']}/{twin['key'][-2:]}", key=f"{r['key']}+{twin['key']}")
            used.add(twin["key"])
        elif r["key"] == "VM_LEFT_CASTER" and "VM_RIGHT_CASTER" in by_key:
            caster_twin = by_key["VM_RIGHT_CASTER"]
            if (caster_twin["before"], caster_twin["after"]) == (r["before"], r["after"]):
                r = dict(r, name="Caster", key="VM_LEFT_CASTER+VM_RIGHT_CASTER")
                used.add("VM_RIGHT_CASTER")
        merged.append(r)
    return merged
