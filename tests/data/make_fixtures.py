"""Cut recorded-telemetry fixtures from the local LMU install (current game build only).

Each fixture is one game recording reduced to the channels the pipeline tests read,
without the driver name and Steam id, stored as compressed numpy arrays (a DuckDB file
per fixture would be mostly empty blocks). ``rebuild`` writes it back as a recording in
the game's format, which the tests open like any other.

    .venv\\Scripts\\python tests\\data\\make_fixtures.py

Interlagos (GT3, build 25661166, 2026-10-23), one car and session, differing in one setting:
  interlagos_wing_11      rear wing 11.3 deg, rear bar P3
  interlagos_wing_8       rear wing 8.3 deg (otherwise as above)
  interlagos_arb_r3       rear wing 12.3 deg, rear bar P3
  interlagos_arb_r5       rear bar P5 (otherwise as above)
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import duckdb
import numpy as np

HERE = Path(__file__).resolve().parent
FIXTURES = {
    "interlagos_wing_11": "Autódromo José Carlos Pace_P_2026-10-23T17_15_06Z.duckdb",
    "interlagos_wing_8": "Autódromo José Carlos Pace_P_2026-10-23T17_24_31Z.duckdb",
    "interlagos_arb_r3": "Autódromo José Carlos Pace_P_2026-10-23T17_29_14Z.duckdb",
    "interlagos_arb_r5": "Autódromo José Carlos Pace_P_2026-10-23T17_36_18Z.duckdb",
}
FIXED = ["GPS Time", "Ground Speed", "Steering Pos", "GPS Latitude", "GPS Longitude", "Lap Dist", "Throttle Pos",
         "Brake Pos", "RideHeights", "Susp Pos", "Path Lateral", "Track Edge"]
EVENTS = ["SurfaceTypes", "Lap", "In Pits", "Gear", "Current Sector"]
PRIVATE = ("DriverName", "SteamID")


def cut(src: Path, dst: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "src.duckdb"
        shutil.copyfile(src, copy)
        con = duckdb.connect(str(copy), read_only=True)
        meta = [r for r in con.execute("select key, value from metadata").fetchall() if r[0] not in PRIVATE]
        channels = [r for r in con.execute('select * from "channelsList"').fetchall() if r[0] in FIXED]
        arrays = {}
        for name in FIXED + EVENTS:
            for col, values in con.execute(f'select * from "{name}"').fetchnumpy().items():
                v = np.asarray(values)
                if v.dtype == np.float64 and name not in ("GPS Time", "GPS Latitude", "GPS Longitude") and col != "ts":
                    v = v.astype(np.float32)
                arrays[f"{name}\t{col}"] = v
        con.close()
    np.savez_compressed(dst, __meta__=np.array(json.dumps({"metadata": meta, "channels": channels})), **arrays)


def rebuild(npz: Path, dst: Path) -> Path:
    """Write a fixture back as a game-format recording at ``dst``."""
    data = np.load(npz, allow_pickle=False)
    meta = json.loads(str(data["__meta__"]))
    con = duckdb.connect(str(dst))
    con.execute("create table metadata (key varchar, value varchar)")
    con.executemany("insert into metadata values (?, ?)", meta["metadata"])
    con.execute('create table "channelsList" (channelName varchar primary key, frequency integer, unit varchar)')
    con.executemany('insert into "channelsList" values (?, ?, ?)', meta["channels"])
    tables: dict[str, dict[str, np.ndarray]] = {}
    for key in data.files:
        if key != "__meta__":
            table, col = key.split("\t")
            tables.setdefault(table, {})[col] = data[key]
    for table, cols in tables.items():
        frame = {c: v.astype(np.float64) if v.dtype == np.float32 else v for c, v in cols.items()}
        con.register("frame", frame)
        con.execute(f'create table "{table}" as select * from frame')
        con.unregister("frame")
    con.close()
    return dst


def main() -> int:
    from autosetup.lmu.paths import find_install

    telemetry = find_install().telemetry
    for name, file in FIXTURES.items():
        src = telemetry / file
        if not src.is_file():
            print(f"missing {file}", file=sys.stderr)
            return 1
        dst = HERE / f"{name}.npz"
        cut(src, dst)
        print(f"{dst.name}: {dst.stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
