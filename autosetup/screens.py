"""Data for the History and Welcome screens.

A *session* is one sitting with one car at one track: consecutive recordings of the same
car + layout with less than SESSION_GAP_S between the end of one and the start of the
next. Its id is the id of its first recording.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from autosetup import analysis
from autosetup.feel.cars import model_of
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
    models = {c: model_of(vc or "", "") for c, vc in store.con.execute("select car_name, vehicle_class from car_classes").fetchall()}
    for s in out:
        s["stints"] = len(s["stint_ids"])
        s["model"] = models.get(s["car_name"])  # e.g. 'BMW_M4_LMGT3'; None until a saved setup names it
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
    # Each stint's best clean lap, per car + layout in the order driven. Only the laps are
    # read, all in one go: building every context's full stint list (summaries, setup
    # diffs) for one sentence took half a second on the Home screen.
    stints = store.con.execute(
        """select id, car_name, coalesce(track_layout, track) from recordings where duration > 30
           order by file_mtime""").fetchall()
    laps_by = analysis.laps_of(store, [rid for rid, _, _ in stints])
    bests_by: dict[tuple[str, str], list[float]] = {}
    for rid, car, layout in stints:
        clean = [lap["time"] for lap in laps_by[rid] if lap["clean"]]
        if clean:
            bests_by.setdefault((car, layout), []).append(min(clean))
    for c in analysis.contexts(store):
        bests = bests_by.get((c.car_name, c.layout), [])
        if len(bests) >= 2 and bests[0] - min(bests) >= 0.2:
            facts.append(f"At {c.layout} your best is {bests[0] - min(bests):.1f} s quicker than in your first stint there.")
    return rng.choice(facts)

