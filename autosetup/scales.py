"""What a setup step means for one car: index → display value, and the direction of travel.

The game only tells us the label at the *current* index of each setting. To show what a
proposed index means ("-2.2 deg → -2.0 deg") we collect every (index, label) pair seen for
the same car model: from all its recordings, and from saved .svm files that match one of
those recordings closely (same car, same game version). Saved setups from other versions
of the same car model add a setting's labels only where they agree with every label this
version has shown for it: updates relabel some settings, and that gives itself away.
Numeric labels are then interpolated; non-numeric ones ("P3",
"D38x1_1-2") are only shown where they've been seen.
"""

from __future__ import annotations

import collections
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from autosetup.feel.cars import model_of
from autosetup.lmu.setup_space import CarSetup, parse_number
from autosetup.lmu.svm import SvmFile, match_keys, score_keys
from autosetup.store import Store

SAME_CAR_SCORE = 0.6  # svm ↔ recording match needed to trust the svm's labels

# Step sizes confirmed on real cars (camber 0.1°, ride height 0.1 cm) or the game's usual
# granularity (pressure 1 kPa). Used only to extrapolate when a single point is known.
DEFAULT_STEP = {"WM_PRESSURE": 1.0, "WM_CAMBER": 0.1, "WM_RIDEHEIGHT": 0.1}


@dataclass
class Scale:
    key: str
    points: dict[int, str] = field(default_factory=dict)  # index -> label

    def _numeric(self) -> list[tuple[int, float]]:
        out = []
        for i, label in sorted(self.points.items()):
            v = _value_of(self.key, label)
            if v is not None:
                out.append((i, v))
        return out

    @property
    def per_step(self) -> float | None:
        """Change in the numeric value per index step (signed), if it can be told."""
        pts = self._numeric()
        if len(pts) >= 2:
            (i0, v0), (i1, v1) = pts[0], pts[-1]
            return (v1 - v0) / (i1 - i0) if i1 != i0 else None
        base = re.sub(r"-W_(FL|FR|RL|RR)$", "", self.key)
        return DEFAULT_STEP.get(base)

    def label(self, index: int) -> tuple[str | None, bool]:
        """(label, exact). Exact when this index has been seen; otherwise interpolated
        from numeric labels (exact=False), or (None, False) if it can't be told."""
        if index in self.points:
            return self.points[index], True
        # Settings labelled by their own step number (TC/ABS maps: '6' at index 6).
        if self.points and all(lbl.strip() == str(i) for i, lbl in self.points.items()):
            return str(index), False
        pts = self._numeric()
        step = self.per_step
        if not pts or step is None:
            return None, False
        # Interpolate between the nearest known points (or extrapolate from the nearest).
        below = [p for p in pts if p[0] < index]
        above = [p for p in pts if p[0] > index]
        if below and above:
            (i0, v0), (i1, v1) = below[-1], above[0]
            v = v0 + (v1 - v0) * (index - i0) / (i1 - i0)
        else:
            i0, v0 = (below[-1] if below else above[0])
            v = v0 + step * (index - i0)
        template = self.points[(below[-1] if below else above[0])[0]]
        return _format_like(self.key, template, v), False


def _value_of(key: str, label: str) -> float | None:
    """Numeric value of a label. Brake balance labels read 'front:rear'; we use front %.
    Diff ramps read 'FF6-30 deg': the ramp angle, the number before 'deg'."""
    ramp = re.search(r"(\d+(?:\.\d+)?)\s*deg\b", label) if "DIFF" in key else None
    if ramp:
        return float(ramp.group(1))
    return parse_number(label)


def _format_like(key: str, template: str, value: float) -> str:
    """Render ``value`` in the style of a known label: same decimals and unit suffix."""
    ramp = re.search(r"(\d+(?:\.\d+)?)(\s*deg\b)", template) if "DIFF" in key else None
    if ramp:  # 'FF6-30 deg': the angle changes, the plates stay
        return f"{template[:ramp.start(1)]}{value:g}{template[ramp.end(1):]}"
    m = re.match(r"\s*([-+]?\d+(?:\.(\d+))?)(.*)$", template)
    if not m:
        return f"{value:g}"
    decimals = len(m.group(2) or "")
    rest = m.group(3)
    if ":" in rest and key == "VM_BRAKE_BALANCE":  # '52.5:47.5' → keep front:rear
        return f"{value:.{decimals}f}:{100 - value:.{decimals}f}"
    rest = re.sub(r"\s*\((soft|hard)\)", "", rest)
    return f"{value:.{decimals}f}{rest}"


class CarScales:
    def __init__(self, scales: dict[str, Scale]):
        self.scales = scales

    def __getitem__(self, key: str) -> Scale:
        return self.scales.setdefault(key, Scale(key))

    def label(self, key: str, index: int) -> tuple[str | None, bool]:
        return self[key].label(index)


def build_scales(store: Store, car_signature: str, settings_dir: Path | None = None,
                 svm_cache: dict[Path, SvmFile] | None = None) -> CarScales:
    rows = store.con.execute(
        "select setup_json from recordings where car_signature = ?", [car_signature]
    ).fetchall()
    setups = [CarSetup.from_json(json.loads(r[0])) for r in rows]
    points: dict[str, dict[int, str]] = collections.defaultdict(dict)
    for st in setups:
        for p in st:
            points[p.key][p.value] = p.text.strip()
    if settings_dir is not None and setups:
        svms = svm_cache if svm_cache is not None else {f: SvmFile.read(f) for f in settings_dir.glob("*/*.svm")}
        recorded = list({tuple(match_keys(st)): None for st in setups})  # many stints share a setup
        for svm in svms.values():
            if not any(score_keys(list(k), svm, SAME_CAR_SCORE) >= SAME_CAR_SCORE for k in recorded):
                continue
            labels, values = svm.vm_labels(), svm.vm_values()
            for k, i in values.items():
                if k in points and k in labels:
                    points[k].setdefault(i, labels[k].strip())  # recordings win on conflicts
        models = {model_of(vc) for (vc,) in store.con.execute(
            """select distinct c.vehicle_class from car_classes c join recordings r using (car_name)
               where r.car_signature = ?""", [car_signature]).fetchall()} - {None}
        other: dict[str, dict[int, set[str]]] = collections.defaultdict(lambda: collections.defaultdict(set))
        for svm in svms.values():
            if model_of(svm.vehicle_class) not in models:
                continue
            labels = svm.vm_labels()
            for k, i in svm.vm_values().items():
                if k in points and k in labels:
                    other[k][i].add(labels[k].strip())
        for k, seen in other.items():
            if any(len(v) > 1 for v in seen.values()):
                continue  # the versions disagree among themselves
            shared = [i for i in seen if i in points[k]]
            if shared and all(seen[i] == {points[k][i]} for i in shared):
                for i, v in seen.items():
                    points[k].setdefault(i, next(iter(v)))
    return CarScales({k: Scale(k, dict(v)) for k, v in points.items()})
