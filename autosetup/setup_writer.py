"""Turn a recorded setup (plus changes) into a new .svm the garage can load.

A recording holds every setting's index but not the .svm header (VehicleClassSetting,
UpgradeSetting, //VEH=), so a saved .svm of the same car model serves as the template.
Every mapped setting is then overwritten from the recording, which makes the template's
own values irrelevant apart from tyre compound (see mapping.DO_NOT_WRITE).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autosetup import templates
from autosetup.lmu.paths import LmuPaths
from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.svm import SvmFile, next_free_name
from autosetup.lmu.tracks import guess_folder
from autosetup.store import Store


class WriteError(Exception):
    pass


# No template or no folder: both come from a setup saved in the garage.
SAVE_FIRST = "Save a setup before driving"


@dataclass
class WriteResult:
    path: Path
    changes: dict[str, tuple[int, int]]  # key -> (from, to)
    template: Path
    notes: str = ""


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
    names: tuple[str, str] | None = None,
    fields: dict | None = None,
) -> WriteResult:
    """``names``: (file name template, description template), templates.py; ``fields``:
    their values, the Settings folder's track name added here."""
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

    template = template or store.template_for(rec["car_signature"], rec["car_name"], paths.settings, setup)
    if template is None:
        raise WriteError(SAVE_FIRST)
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
    folder_name = store.folder_for(rec["track"])
    folder = paths.settings / folder_name if folder_name else guess_folder(rec["track"], paths.settings)
    if folder is None:
        raise WriteError(SAVE_FIRST)
    name_t, notes_t = names or (templates.NAME, templates.NOTES)
    fields = {**base_fields(store, rec), "track": folder.name, **(fields or {})}
    if "changes" not in fields:  # without the proposal's labels and reasons: names and steps
        from autosetup.analysis import pretty_name
        fields["changes"] = "; ".join(f"{pretty_name(k)} {setup[k].text.strip()} {b - a:+d}" for k, (a, b) in applied.items())
    notes = templates.render(notes_t, {**fields, "lap": fields.get("lap_notes", fields.get("lap", ""))})
    svm.set_notes(notes)

    def name_at(n: int) -> str:
        name = templates.file_safe(templates.render(name_t, {**fields, "n": n}))
        return name if "{n}" in name_t or n == 1 else f"{name} {n}"
    target = next_free_name(folder, name_at)
    if not dry_run:
        svm.write(target)
    return WriteResult(target, applied, template, notes)


def _still_symmetric(values: dict[str, int]) -> bool:
    for left, right in (("FL", "FR"), ("RL", "RR")):
        for k, v in values.items():
            if k.endswith(f"-W_{left}") and not k.startswith("WM_COMPOUND"):
                if values.get(k[: -len(left)] + right, v) != v:
                    return False
    return True


def base_fields(store: Store, rec: dict) -> dict:
    """The template fields every setup has: the car model (never the team), class, date, time."""
    from autosetup.feel.cars import model_of

    row = store.con.execute("select vehicle_class from car_classes where car_name = ?", [rec["car_name"]]).fetchone()
    model = model_of(row[0], rec["car_class"] or "") if row and row[0] else None
    now = datetime.now()
    return {"car": templates.car_name(model), "class": rec["car_class"] or "", "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H-%M")}
