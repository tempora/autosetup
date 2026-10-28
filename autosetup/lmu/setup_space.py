"""The setup parameter space of one car, parsed from a recording's ``CarSetup`` JSON.

Each entry looks like::

    "WM_CAMBER-W_FL": {"available": true, "minValue": 0, "maxValue": 33, "value": 28,
                       "stringValue": "-2.2 deg", "lastSavedStringValue": "-5.0 deg", ...}

``value`` is the same step index the .svm file stores. ``maxValue`` is exclusive: it's
the number of steps (e.g. brake pressure has maxValue 81 and "100%" sits at index 80;
fixed parameters have 0..1). ``minValue`` is inclusive (ride heights start at 2).

Keys are ``VM_<NAME>`` for car-wide parameters and ``WM_<NAME>-W_<FL|FR|RL|RR>`` for
per-wheel ones.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cached_property

CORNERS = ("FL", "FR", "RL", "RR")
_NUMBER = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")


@dataclass(frozen=True)
class Param:
    key: str
    value: int
    min: int  # inclusive
    max: int  # exclusive
    text: str  # display value at ``value``, e.g. "-2.2 deg"
    available: bool
    last_saved_text: str = ""

    @property
    def steps(self) -> int:
        return self.max - self.min

    @property
    def adjustable(self) -> bool:
        return self.available and self.steps > 1

    @property
    def corner(self) -> str | None:
        """FL/FR/RL/RR for per-wheel parameters, else None."""
        m = re.search(r"-W_(FL|FR|RL|RR)$", self.key)
        return m.group(1) if m else None

    @property
    def base(self) -> str:
        """Key without the corner suffix: 'WM_CAMBER-W_FL' → 'WM_CAMBER'."""
        return re.sub(r"-W_(FL|FR|RL|RR)$", "", self.key)

    @property
    def fraction(self) -> float:
        """Position within the range, 0..1, for optimisers that work car-independently."""
        return (self.value - self.min) / (self.steps - 1) if self.steps > 1 else 0.0

    def clamp(self, index: int) -> int:
        return max(self.min, min(self.max - 1, int(index)))

    @property
    def number(self) -> float | None:
        return parse_number(self.text)


def parse_number(text: str) -> float | None:
    """First number in a display string: '-2.2 deg' → -2.2, '52.5:47.5' → 52.5,
    '8,500' → 8500, '1 (soft)' → 1. Labels without numbers ('Open', 'D35x1_1-1') → None
    unless they start with a number."""
    s = text.strip()
    m = _NUMBER.match(s) or (None if re.match(r"[A-Za-z]", s) else _NUMBER.search(s))
    return float(m.group(0).replace(",", "")) if m else None


class CarSetup:
    """All parameters of one setup, plus helpers to treat it as a search space."""

    def __init__(self, params: dict[str, Param], symmetric: bool = True, gear_graph: dict | None = None):
        self.params = params
        self.symmetric = symmetric
        self.gear_graph = gear_graph or {}

    @classmethod
    def from_json(cls, data: dict) -> "CarSetup":
        params = {}
        for key, v in data.items():
            if not isinstance(v, dict) or "minValue" not in v:
                continue
            params[key] = Param(
                key=key,
                value=int(v["value"]),
                min=int(v["minValue"]),
                max=int(v["maxValue"]),
                text=str(v.get("stringValue", "")),
                available=bool(v.get("available", False)),
                last_saved_text=str(v.get("lastSavedStringValue", "")),
            )
        return cls(params, bool(data.get("symmetric", True)), data.get("gearGraph"))

    def __getitem__(self, key: str) -> Param:
        return self.params[key]

    def __contains__(self, key: str) -> bool:
        return key in self.params

    def __iter__(self):
        return iter(self.params.values())

    def values(self) -> dict[str, int]:
        return {k: p.value for k, p in self.params.items()}

    def adjustable(self) -> list[Param]:
        """Parameters the garage lets you change (more than one step). The current label
        may still read 'Detached' or 'Off' – that is just the value at this step."""
        return [p for p in self.params.values() if p.adjustable]

    @cached_property
    def signature(self) -> str:
        """Stable id of the car *model*: identical for every livery of the same car,
        different as soon as the parameter ranges differ (other car, other BoP/upgrade)."""
        parts = sorted(f"{p.key}:{p.min}:{p.max}" for p in self.params.values() if p.available)
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]

    @property
    def steering_ratio(self) -> float | None:
        """Steering ratio from the steer-lock label, e.g. '560 (16.5) deg' → 16.5."""
        p = self.params.get("VM_STEER_LOCK")
        m = re.search(r"\(([\d.]+)\)", p.text) if p else None
        return float(m.group(1)) if m else None
