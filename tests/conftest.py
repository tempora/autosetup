"""Synthetic LMU install for tests: a recording shaped like the game's and a matching .svm."""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import numpy as np
import pytest

from autosetup.lmu.paths import LmuPaths

T0 = 1000.0  # session time when recording starts
LAP = 60.0  # seconds per lap
LAP_LEN = 3000.0  # metres
DURATION = 200.0  # start mid-lap 1, laps 2 and 3 complete, end mid-lap 4

# A cut-down CarSetup in the game's shape (maxValue is exclusive).
SETUP = {
    "VM_REAR_ANTISWAY": dict(minValue=0, maxValue=8, value=3, stringValue="P4"),
    "VM_FRONT_ANTISWAY": dict(minValue=0, maxValue=8, value=5, stringValue="P6"),
    "VM_BRAKE_BALANCE": dict(minValue=0, maxValue=57, value=18, stringValue="52.5:47.5"),
    "VM_STEER_LOCK": dict(minValue=0, maxValue=8, value=7, stringValue="560 (16.5) deg"),
    "VM_GEAR_1": dict(minValue=0, maxValue=1, value=0, stringValue="Fixed"),
    "WM_CAMBER-W_FL": dict(minValue=0, maxValue=33, value=28, stringValue="-2.2 deg"),
    "WM_CAMBER-W_FR": dict(minValue=0, maxValue=33, value=28, stringValue="-2.2 deg"),
    "WM_RIDEHEIGHT-W_RL": dict(minValue=2, maxValue=51, value=13, stringValue="6.3 cm"),
    "WM_RIDEHEIGHT-W_RR": dict(minValue=2, maxValue=51, value=13, stringValue="6.3 cm"),
    "WM_COMPOUND-W_FL": dict(minValue=0, maxValue=4, value=1, stringValue="Medium"),
}

SVM_TEXT = "\r\n".join([
    'VehicleClassSetting="GT3 Test_Car WEC2025"',
    "UpgradeSetting=(123,0,0,0)",
    r"//VEH=C:\Fake\Vehicles\Test_Car\1.0\TEST.VEH",
    "//Note: settings commented out if using the default",
    "",
    "[GENERAL]",
    'Notes=""',
    "Symmetric=1",
    "",
    "[SUSPENSION]",
    "FrontAntiSwaySetting=5//P6",
    "RearAntiSwaySetting=3//P4",
    "",
    "[CONTROLS]",
    "SteerLockSetting=7//560 (16.5) deg",
    "RearBrakeSetting=18//52.5:47.5",
    "",
    "[DRIVELINE]",
    "Gear1Setting=0//Fixed",
    "",
    "[FRONTLEFT]",
    "CamberSetting=28//-2.2 deg",
    "CompoundSetting=0//96% Medium",
    "",
    "[FRONTRIGHT]",
    "CamberSetting=28//-2.2 deg",
    "",
    "[REARLEFT]",
    "RideHeightSetting=13//6.3 cm",
    "",
    "[REARRIGHT]",
    "RideHeightSetting=13//6.3 cm",
    "",
    "[BASIC]",
    "Downforce=0.500000",
    "",
])


def setup_json() -> dict:
    data = {k: {"available": True, "isFreeSetting": True, "key": k, **v} for k, v in SETUP.items()}
    data["symmetric"] = True
    data["gearGraph"] = {"numForwardGears": 6}
    return data


def _with_values(data: dict, values: dict[str, int]) -> dict:
    for k, v in values.items():
        data[k]["value"] = v
        data[k]["stringValue"] = f"step {v}"
    return data


def make_recording(path: Path, lap_dist_hz: int = 50, laps: int = 2, values: dict[str, int] | None = None,
                   track: str = "Test Raceway", mtime: float | None = None, tyres: bool = False) -> Path:
    """Write a recording the way the game does: fixed-rate tables without timestamps,
    event tables with ``ts``, channel rates in ``channelsList``.

    ``laps`` complete laps sit between a partial first lap and a final in-pit lap (the
    default matches DURATION); ``values`` overrides setup indices; ``mtime`` sets the
    file time stints are ordered by; ``tyres``: add speed and tread temperatures with
    the front inner edges 16 °C hotter than the outer (a camber rule to fire)."""
    duration = DURATION + (laps - 2) * LAP
    con = duckdb.connect(str(path))
    clock = T0 + np.arange(int(duration * 100) + 1) / 100.0

    def fixed(name: str, hz: int, cols: dict[str, np.ndarray]) -> None:
        typ = "DOUBLE" if name == "GPS Time" else "FLOAT"
        con.execute(f'create table "{name}" ({", ".join(f"{c} {typ}" for c in cols)})')
        # One bulk insert; row-by-row executemany takes minutes for 20k rows.
        unnest = ", ".join(f"unnest(${i + 1}::DOUBLE[])" for i in range(len(cols)))
        con.execute(f'insert into "{name}" select {unnest}', [v.tolist() for v in cols.values()])
        con.execute("insert into channelsList values (?, ?, ?)", [name, hz, ""])

    def events(name: str, rows: list[tuple[float, int]]) -> None:
        con.execute(f'create table "{name}" (ts DOUBLE, value INTEGER)')
        con.executemany(f'insert into "{name}" values (?, ?)', rows)

    con.execute("create table channelsList (channelName VARCHAR, frequency INTEGER, unit VARCHAR)")
    con.execute("create table metadata (key VARCHAR, value VARCHAR)")
    fixed("GPS Time", 100, {"value": clock})
    t_ld = T0 + np.arange(int(duration * lap_dist_hz) + 1) / lap_dist_hz
    # Recording starts 30 s into lap 1, so crossings are at T0+30, T0+90, T0+150.
    fixed("Lap Dist", lap_dist_hz, {"value": ((t_ld - T0 + 30.0) % LAP) / LAP * LAP_LEN})
    t10 = T0 + np.arange(int(duration * 10) + 1) / 10.0
    wear = np.column_stack([1.0 - (t10 - T0) * k for k in (1e-4, 2e-4, 3e-4, 4e-4)])
    fixed("Tyres Wear", 10, {f"value{i + 1}": wear[:, i] for i in range(4)})
    if tyres:
        fixed("Ground Speed", 100, {"value": np.full(len(clock), 144.0)})
        n10 = len(t10)
        inner, outer = {"F": 90.0, "R": 85.0}, {"F": 74.0, "R": 76.0}
        centre = {"F": 87.0, "R": 85.5}
        # Car-frame sides: the inner edge is Right on the left wheels, Left on the right ones.
        left = [outer["F"], inner["F"], outer["R"], inner["R"]]
        right = [inner["F"], outer["F"], inner["R"], outer["R"]]
        for name, vals in (("TyresTempLeft", left), ("TyresTempRight", right),
                           ("TyresTempCentre", [centre["F"], centre["F"], centre["R"], centre["R"]])):
            fixed(name, 10, {f"value{i + 1}": np.full(n10, v) for i, v in enumerate(vals)})
    events("Lap", [(T0, 1)] + [(T0 + 30.0 + LAP * k, 2 + k) for k in range(laps + 1)])
    events("In Pits", [(T0, 0), (T0 + 30.0 + LAP * laps + 5.0, 1)])
    meta = {
        "Version": "1", "DriverName": "Test Driver", "TrackName": track, "TrackLayout": track,
        "CarName": "Test Team 2025 #1:LM", "CarClass": "GT3", "SessionType": "Practice",
        "RecordingTime": "2026-01-01T10_00_00Z", "WeatherConditions": "Clear",
        "CarSetup": json.dumps(_with_values(setup_json(), values or {})),
    }
    con.executemany("insert into metadata values (?, ?)", list(meta.items()))
    con.close()
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def recording(tmp_path) -> Path:
    return make_recording(tmp_path / "Test Raceway_P_2026-01-01T10_00_00Z.duckdb")


@pytest.fixture
def install(tmp_path) -> LmuPaths:
    """Fake install: one recording, one saved setup in a folder not named like the track."""
    root = tmp_path / "Le Mans Ultimate"
    paths = LmuPaths(root)
    paths.telemetry.mkdir(parents=True)
    (paths.settings / "Testway").mkdir(parents=True)
    make_recording(paths.telemetry / "Test Raceway_P_2026-01-01T10_00_00Z.duckdb")
    (paths.settings / "Testway" / "base.svm").write_bytes(SVM_TEXT.encode("latin-1"))
    return paths


# -- recorded telemetry (tests/data, current game build only) -----------------------------
DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture(scope="session")
def recorded(tmp_path_factory):
    """``recorded(name)`` → path of that fixture rebuilt as a game recording (see
    tests/data/make_fixtures.py), under its original file name."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_fixtures", DATA / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = tmp_path_factory.mktemp("recorded")
    built: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name not in built:
            built[name] = mod.rebuild(DATA / f"{name}.npz", root / mod.FIXTURES[name])
        return built[name]

    return get
