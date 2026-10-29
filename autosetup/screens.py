"""Data for the History, Welcome and Setup screens (UI_LAYOUT_CONTRACT.md §2, §4, §5).

A *session* is one sitting with one car at one track: consecutive recordings of the same
car + layout with less than SESSION_GAP_S between the end of one and the start of the
next. Its id is the id of its first recording.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from autosetup import analysis
from autosetup.analysis import WHEELS
from autosetup.store import Store

SESSION_GAP_S = 2 * 3600


def _started(recorded_at: str | None, fallback: float) -> float:
    try:
        return datetime.strptime(recorded_at or "", "%Y-%m-%dT%H_%M_%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return fallback


def sessions(store: Store) -> list[dict]:
    """Every session, newest first."""
    rows = store.con.execute(
        """select r.id, r.car_name, r.car_class, r.track, coalesce(r.track_layout, r.track), r.recorded_at,
                  r.file_mtime, r.duration,
                  count(l.seq) filter (where l.complete and not l.in_pits),
                  min(l.lap_time) filter (where l.complete and not l.in_pits)
           from recordings r left join laps l on l.recording_id = r.id
           where r.duration > 30 group by all order by r.file_mtime""").fetchall()
    open_: dict[str, dict] = {}
    out: list[dict] = []
    for rid, car, car_class, track, layout, recorded_at, mtime, duration, laps, best in rows:
        key = f"{car}|{layout}"
        start = _started(recorded_at, mtime - duration)
        cur = open_.get(key)
        if cur is None or start - cur["_end"] > SESSION_GAP_S:
            cur = {"id": rid, "context": key, "car_name": car, "car_class": car_class, "track": track,
                   "layout": layout, "recorded_at": recorded_at, "stint_ids": [], "laps": 0, "best": None, "_end": 0.0}
            open_[key] = cur
            out.append(cur)
        cur["stint_ids"].append(rid)
        cur["laps"] += laps or 0
        if best is not None and (cur["best"] is None or best < cur["best"]):
            cur["best"] = best
        cur["_end"] = max(cur["_end"], start + duration)
    for s in out:
        s["stints"] = len(s["stint_ids"])
        s.pop("_end")
    return sorted(out, key=lambda s: s["stint_ids"][-1], reverse=True)


def session(store: Store, session_id: int) -> dict:
    for s in sessions(store):
        if s["id"] == session_id:
            return s
    raise KeyError(f"no session {session_id}")


def latest_session(store: Store, context: str) -> dict | None:
    return next((s for s in sessions(store) if s["context"] == context), None)


# -- Welcome --------------------------------------------------------------------------------
def fun_fact(store: Store, rng: random.Random | None = None) -> str | None:
    """One true sentence about the user's own driving, or None with no laps yet."""
    rng = rng or random.Random()
    laps = store.con.execute(
        """select r.car_name, coalesce(r.track_layout, r.track), r.file_mtime, l.lap_time
           from laps l join recordings r on r.id = l.recording_id
           where l.complete and not l.in_pits and r.duration > 30 order by r.file_mtime""").fetchall()
    if not laps:
        return None
    facts = []
    by_track: dict[str, int] = {}
    by_car: dict[str, int] = {}
    for car, layout, _, _ in laps:
        by_track[layout] = by_track.get(layout, 0) + 1
        by_car[car] = by_car.get(car, 0) + 1
    track, n = max(by_track.items(), key=lambda kv: kv[1])
    facts.append(f"You've done {n} laps at {track}.")
    car, n = max(by_car.items(), key=lambda kv: kv[1])
    if len(by_car) > 1:
        facts.append(f"Your most driven car is the {car}: {n} laps.")
    facts.append(f"{len(laps)} laps so far, with {len(by_car)} car{'s' if len(by_car) != 1 else ''} "
                 f"at {len(by_track)} track{'s' if len(by_track) != 1 else ''}.")
    for c in analysis.contexts(store):
        st = analysis.stints(store, c.car_name, c.layout)
        bests = [s.summary["best"] for s in st if s.summary["best"] is not None]
        if len(bests) >= 2 and bests[0] - min(bests) >= 0.2:
            facts.append(f"At {c.layout} your best is {bests[0] - min(bests):.1f} s quicker than in your first stint there.")
    return rng.choice(facts)


# -- Setup table ----------------------------------------------------------------------------
# (group, label, key, unit, decimals, scale); a key "<name>_{w}" expands to the four wheels.
ROWS = [
    ("Pace", "Lap time", "time", "s", 3, 1),
    ("Pace", "Best", "best", "s", 3, 1),
    ("Pace", "Median", "median", "s", 3, 1),
    ("Pace", "Theoretical best", "theoretical_best", "s", 3, 1),
    ("Pace", "Sector 1", "s1", "s", 3, 1),
    ("Pace", "Sector 2", "s2", "s", 3, 1),
    ("Pace", "Sector 3", "s3", "s", 3, 1),
    ("Pace", "Spread", "consistency", "s", 3, 1),
    ("Pace", "Clean laps", "clean_laps", "", 0, 1),
    ("Pace", "Top speed", "speed_max", "km/h", 1, 1),
    ("Pace", "Min speed", "speed_min", "km/h", 1, 1),
    ("Balance", "Understeer turn-in", "us_entry", "°", 2, 1),
    ("Balance", "Understeer apex", "us_mid", "°", 2, 1),
    ("Balance", "Understeer exit", "us_exit", "°", 2, 1),
    ("Balance", "Understeer slow corners", "us_slow", "°", 2, 1),
    ("Balance", "Understeer fast corners", "us_fast", "°", 2, 1),
    ("Balance", "Countersteer", "countersteer_events", "/lap", 1, 1),
    ("Balance", "Countersteer turn-in", "cs_entry", "/lap", 1, 1),
    ("Balance", "Countersteer apex", "cs_mid", "/lap", 1, 1),
    ("Balance", "Countersteer exit", "cs_exit", "/lap", 1, 1),
    ("Balance", "Steering reversals", "steer_reversals_per_min", "/min", 1, 1),
    ("Tyres", "Inner {W}", "t_inner_{w}", "°C", 1, 1),
    ("Tyres", "Centre {W}", "t_centre_{w}", "°C", 1, 1),
    ("Tyres", "Outer {W}", "t_outer_{w}", "°C", 1, 1),
    ("Tyres", "Spread {W}", "t_spread_{w}", "°C", 1, 1),
    ("Tyres", "Crown {W}", "t_crown_{w}", "°C", 1, 1),
    ("Tyres", "Carcass {W}", "t_carcass_{w}", "°C", 1, 1),
    ("Tyres", "Hot pressure {W}", "p_hot_{w}", "kPa", 1, 1),
    ("Tyres", "Wear {W}", "wear_{w}", "%/lap", 2, 1),
    ("Tyres", "Overheating", "overheating_s", "s", 1, 1),
    ("Brakes", "Peak temp {W}", "brake_temp_max_{w}", "°C", 0, 1),
    ("Platform", "Min ride height {W}", "rh_min_{w}", "mm", 1, 1),
    ("Platform", "Bottoming", "bottoming_s", "s", 2, 1),
    ("Platform", "Rake fast", "rake_fast", "mm", 1, 1),
    ("Platform", "Kerbs", "kerb_s", "s", 1, 1),
    ("Platform", "3rd travel F", "third_travel_f", "mm", 1, 1),
    ("Platform", "3rd travel R", "third_travel_r", "mm", 1, 1),
    ("Platform", "On 3rd packers F", "third_packers_f", "%", 0, 100),
    ("Platform", "On 3rd packers R", "third_packers_r", "%", 0, 100),
    ("Platform", "3rd packer contact F", "third_contact_f", "mm", 1, 1),
    ("Platform", "3rd packer contact R", "third_contact_r", "mm", 1, 1),
    ("Aids", "TC active", "tc_s", "s", 1, 1),
    ("Aids", "ABS active", "abs_s", "s", 1, 1),
    ("Driving", "Full throttle", "full_throttle_frac", "%", 0, 100),
    ("Driving", "Coasting", "coast_frac", "%", 0, 100),
    ("Driving", "Trail braking", "trail_brake_frac", "%", 0, 100),
    ("Driving", "Throttle rate", "throttle_apply_rate", "/s", 2, 1),
    ("Driving", "Peak braking", "brake_g_p98", "g", 2, 1),
    ("Driving", "Peak lateral", "lat_g_p98", "g", 2, 1),
    ("Driving", "Fuel used", "fuel_used", "L", 2, 1),
    ("Conditions", "Track temp", "track_temp", "°C", 1, 1),
]


# Hover text per row key ("{w}" rows share one); one or two plain sentences, which way is good.
HELP = {
    "consistency": "How far your clean laps spread. Lower is steadier.",
    "theoretical_best": "Your best sectors added up.",
    "us_entry": "Understeer angle at turn-in, in degrees of steering. + pushes, − rotates.",
    "us_mid": "Understeer angle at the apex, in degrees of steering. + pushes, − rotates.",
    "us_exit": "Understeer angle on exit, in degrees of steering. + pushes, − rotates.",
    "us_slow": "Understeer angle in slow corners. + pushes, − rotates.",
    "us_fast": "Understeer angle in fast corners. + pushes, − rotates.",
    "countersteer_events": "Times per lap you caught a slide with opposite lock. Fewer is calmer.",
    "steer_reversals_per_min": "Steering corrections per minute. Fewer is calmer.",
    "t_spread_{w}": "Inner minus outer tyre temperature. Mostly a camber sign.",
    "t_crown_{w}": "Centre minus the edges' average. + over-inflated, − under-inflated.",
    "t_carcass_{w}": "Temperature inside the tyre, slower to change than the surface.",
    "wear_{w}": "Tread worn per lap.",
    "overheating_s": "Time per lap a tyre was above its window. Lower is better.",
    "rh_min_{w}": "Lowest ride height at that corner over the lap.",
    "bottoming_s": "Time per lap the floor touched the ground. Zero is the aim.",
    "rake_fast": "Rear minus front ride height at speed.",
    "kerb_s": "Time per lap on kerbs.",
    "third_packers_f": "Share of the lap the front third spring sits on its packers.",
    "third_packers_r": "Share of the lap the rear third spring sits on its packers.",
    "coast_frac": "Share of the lap on neither pedal.",
    "trail_brake_frac": "Share of braking done while already turning.",
    "throttle_apply_rate": "How fast you open the throttle out of corners.",
    "brake_g_p98": "Deceleration under your hardest braking.",
    "lat_g_p98": "Sideways grip at its peak.",
}


# What a row's number is, for the display units (units.py): temperature differences
# convert without the offset.
QUANTITY = {"km/h": "speed", "°C": "temp", "kPa": "pressure"}
DELTA_TEMP = ("t_spread_", "t_crown_")


def _quantity(key: str, unit: str) -> str | None:
    q = QUANTITY.get(unit)
    return "dtemp" if q == "temp" and key.startswith(DELTA_TEMP) else q


def _rows() -> list[tuple]:
    out = []
    for group, label, key, unit, dec, scale in ROWS:
        tip = HELP.get(key)
        if "{w}" in key:
            out += [(group, label.replace("{W}", w.upper()), key.replace("{w}", w), unit, dec, scale, tip) for w in WHEELS]
        else:
            out.append((group, label, key, unit, dec, scale, tip))
    return out


def setup_table(store: Store, stint_ids: list[int], by: str = "stint") -> dict:
    """Columns are the stints (or their racing laps), oldest first; rows are grouped
    metrics, then the settings that differ between the columns."""
    if by not in ("stint", "lap"):
        raise ValueError("by must be stint or lap")
    stint_no = {rid: i + 1 for i, rid in enumerate(stint_ids)}
    columns, values, setups = [], [], []
    laps_by = analysis.laps_of(store, stint_ids)
    summary_by = analysis.summaries(store, stint_ids, laps_by) if by == "stint" else {}
    for rid in stint_ids:
        laps = laps_by[rid]
        setup = store.setup_of(rid)
        if by == "stint":
            s = summary_by[rid]
            if not s["racing_laps"]:
                continue
            columns.append({"stint": rid, "seq": None, "label": f"Stint {stint_no[rid]}"})
            values.append(s)
            setups.append(setup)
            continue
        metrics: dict[int, dict] = {}
        for seq, name, value in store.con.execute(
                "select seq, name, value from lap_metrics where recording_id = ?", [rid]).fetchall():
            metrics.setdefault(seq, {})[name] = value
        sectors: dict[int, dict] = {}
        for seq, sector, t in store.con.execute(
                "select seq, sector, time from lap_sectors where recording_id = ?", [rid]).fetchall():
            sectors.setdefault(seq, {})[f"s{sector + 1}"] = t
        for lap in laps:
            if not lap["complete"] or lap["in_pits"]:
                continue
            columns.append({"stint": rid, "seq": lap["seq"], "label": f"{stint_no[rid]}·{lap['lap']}",
                            "clean": lap["clean"]})
            values.append({**metrics.get(lap["seq"], {}), **sectors.get(lap["seq"], {}), "time": lap["time"]})
            setups.append(setup)
    groups: list[dict] = []
    for group, label, key, unit, dec, scale, tip in _rows():
        vals = [v.get(key) for v in values]
        if all(x is None for x in vals):
            continue
        row = {"label": label, "unit": unit, "dec": dec, "quantity": _quantity(key, unit),
               "values": [None if x is None else float(x) * scale for x in vals]}
        if tip:
            row["help"] = tip
        if not groups or groups[-1]["name"] != group:
            groups.append({"name": group, "rows": []})
        groups[-1]["rows"].append(row)
    changed = sorted({k for s in setups for k, p in s.params.items() if p.adjustable
                      and len({(t.params[k].value if k in t.params else None) for t in setups}) > 1})
    if changed:
        rows = [{"label": analysis.pretty_name(k), "unit": "", "dec": 0,
                 "values": [s.params[k].text.strip() if k in s.params else None for s in setups]} for k in changed]
        groups.append({"name": "Setup", "rows": rows})
    return {"by": by, "columns": columns, "groups": groups}
