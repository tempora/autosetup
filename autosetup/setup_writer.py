"""Turn a recorded setup (plus changes) into a new .svm the garage can load.

A recording holds every setting's index but not the .svm header (VehicleClassSetting,
UpgradeSetting, //VEH=), so a saved .svm of the same car model serves as the template.
Every mapped setting is then overwritten from the recording, which makes the template's
own values irrelevant apart from tyre compound (see mapping.DO_NOT_WRITE).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from autosetup.lmu.paths import LmuPaths
from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.svm import SvmFile, next_free_name
from autosetup.lmu.tracks import guess_folder
from autosetup.store import Store


class WriteError(Exception):
    pass


@dataclass
class WriteResult:
    path: Path
    changes: dict[str, tuple[int, int]]  # key -> (from, to)
    template: Path


def parse_change(spec: str, setup: CarSetup) -> tuple[str, int]:
    """'KEY=12' (absolute index), 'KEY+=2' or 'KEY-=1' (relative steps) → (key, new index)."""
    m = re.fullmatch(r"([A-Z0-9_]+(?:-W_(?:FL|FR|RL|RR))?)\s*([+-]?=)\s*(-?\d+)", spec.strip())
    if not m:
        raise WriteError(f"bad change {spec!r}; use KEY=index, KEY+=steps or KEY-=steps")
    key, op, n = m.group(1), m.group(2), int(m.group(3))
    if key not in setup:
        raise WriteError(f"{key} is not a setting of this car")
    cur = setup[key].value
    return key, {"=": n, "+=": cur + n, "-=": cur - n}[op]


def write_setup(
    store: Store,
    paths: LmuPaths,
    recording_id: int,
    changes: dict[str, int] | None = None,
    template: Path | None = None,
    dry_run: bool = False,
) -> WriteResult:
    rec = store.recording(recording_id)
    setup = store.setup_of(recording_id)
    changes = changes or {}

    applied: dict[str, tuple[int, int]] = {}
    for key, new in changes.items():
        p = setup[key]
        if not p.adjustable:
            raise WriteError(f"{key} is not adjustable on this car")
        if not p.min <= new < p.max:
            raise WriteError(f"{key}={new} is outside {p.min}..{p.max - 1}")
        if new != p.value:
            applied[key] = (p.value, new)

    template = template or store.template_for(rec["car_signature"], rec["car_name"], paths.settings)
    if template is None:
        raise WriteError(
            "No saved setup for this car yet. Save any setup for it in the garage once, "
            "then run `autosetup import` so autosetup can use it as a template."
        )
    svm = SvmFile.read(template)

    values = setup.values()
    values.update({k: new for k, (_, new) in applied.items()})
    # Unchanged settings keep the game's own label; changed ones get none (the game
    # ignores comments and shows the real value in the garage).
    labels = {k: p.text for k, p in setup.params.items() if k not in applied}
    svm.set_vm(values, labels)

    symmetric = setup.symmetric and _still_symmetric(values)
    if svm.get("GENERAL", "Symmetric"):
        svm.set("GENERAL", "Symmetric", int(symmetric))
    summary = ", ".join(f"{k} {a}->{b}" for k, (a, b) in applied.items()) or "no changes"
    svm.set_notes(f"autosetup from {rec['file_name']}: {summary}")

    folder_name = store.folder_for(rec["track"])
    folder = paths.settings / folder_name if folder_name else guess_folder(rec["track"], paths.settings)
    if folder is None:
        raise WriteError(f"Don't know the Settings folder for {rec['track']!r}; save a setup there once")
    stem = f"{_short(rec['car_name'])}_{folder.name}"
    target = next_free_name(folder, stem)
    if not dry_run:
        svm.write(target)
    return WriteResult(target, applied, template)


def _still_symmetric(values: dict[str, int]) -> bool:
    for left, right in (("FL", "FR"), ("RL", "RR")):
        for k, v in values.items():
            if k.endswith(f"-W_{left}") and not k.startswith("WM_COMPOUND"):
                if values.get(k[: -len(left)] + right, v) != v:
                    return False
    return True


def _short(car_name: str) -> str:
    """'Iron Lynx 2024 #60' → 'IronLynx60'."""
    words = re.sub(r"\b(19|20)\d\d\b", "", car_name)
    return re.sub(r"[^A-Za-z0-9]", "", words)[:24] or "car"
