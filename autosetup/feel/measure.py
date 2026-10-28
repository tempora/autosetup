"""The balance a stint (or several on one setup) measured.

Cells are phase × speed band ('entry_low' … 'exit_high'), understeer angle in degrees
(+ understeer), pooled from the corner passes (metrics/balance.py). Two extras go with
them: turn-in response (median |yaw| / |r_ref| over the first 0.4 s of turn-in) and
countersteer (seconds per lap, the rear's snap margin).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from autosetup.metrics.balance import Cell, PhaseObs, cells, corner_mix, normalise
from autosetup.metrics.corners import BANDS, PHASES

CELLS = tuple(f"{ph}_{b}" for ph in PHASES for b in BANDS)


def cell_parts(cell: str) -> tuple[str, str]:
    ph, b = cell.split("_", 1)
    return ph, b


@dataclass
class Measured:
    cells: dict[str, Cell] = field(default_factory=dict)
    turnin: float | None = None  # |r| / |r_ref| on turn-in
    countersteer: float | None = None  # s per lap
    mix: dict[str, float] = field(default_factory=lambda: {b: 0.5 for b in BANDS})  # cornering time per band
    passes: int = 0

    def mean(self) -> dict[str, float]:
        return {c: v.mean for c, v in self.cells.items()}

    def to_json(self) -> dict:
        return {"cells": {c: [v.mean, v.se, v.passes] for c, v in self.cells.items()}, "turnin": self.turnin,
                "countersteer": self.countersteer, "mix": self.mix, "passes": self.passes}

    @classmethod
    def from_json(cls, d: dict) -> "Measured":
        return cls({c: Cell(*v) for c, v in d["cells"].items()}, d.get("turnin"), d.get("countersteer"),
                   d.get("mix") or {b: 0.5 for b in BANDS}, d.get("passes", 0))


def passes_of(store, rids: list[int]) -> list[tuple[int, int, PhaseObs]]:
    """(recording id, lap seq, observation) of these recordings."""
    if not rids:
        return []
    cur = store.con.execute("select * from corner_passes where recording_id in (select unnest(?::INTEGER[]))", [rids])
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        rid, seq = d.pop("recording_id"), d.pop("seq")
        out.append((rid, seq, PhaseObs(**d)))
    return out


def measured(store, rids: list[int]) -> Measured:
    """Pooled over the recordings (one setup's stints at one layout)."""
    rows = passes_of(store, rids)
    obs = normalise([o for _, _, o in rows])
    if not obs:
        return Measured()
    c = {f"{ph}_{b}": v for (ph, b), v in cells(obs).items()}
    gains = [o.turnin_gain for o in obs if o.turnin_gain is not None and np.isfinite(o.turnin_gain)]
    laps = {(rid, seq) for rid, seq, _ in rows}
    cs = sum(o.countersteer_s for o in obs) / max(len(laps), 1)
    return Measured(c, float(np.median(gains)) if gains else None, float(cs), corner_mix(obs), len(obs))


def same_setup_stints(store, rid: int) -> list[int]:
    """This recording and the earlier ones in a row before it with the same car, layout
    and setup (one setup driven over several outings is one measurement)."""
    import json

    from autosetup.lmu.setup_space import CarSetup
    from autosetup.opt.space import setup_fingerprint

    car, layout, mtime = store.con.execute(
        "select car_name, coalesce(track_layout, track), file_mtime from recordings where id = ?", [rid]).fetchone()
    rows = store.con.execute(
        """select id, setup_json from recordings where car_name = ? and coalesce(track_layout, track) = ?
           and file_mtime <= ? order by file_mtime desc""", [car, layout, mtime]).fetchall()
    out, first = [], None
    for other, sj in rows:
        fp = setup_fingerprint(CarSetup.from_json(json.loads(sj)))
        if first is None:
            first = fp
        if fp != first:
            break
        out.append(other)
    return out
