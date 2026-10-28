"""Per-class targets and constants (classes.toml): a [default] section plus per-class
overrides matched by prefix (``LMP2`` covers ``LMP2_ELMS``)."""

from __future__ import annotations

import tomllib
from pathlib import Path

KNOWLEDGE = Path(__file__).resolve().parent / "classes.toml"


def load_knowledge(car_class: str, path: Path = KNOWLEDGE) -> dict:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = dict(data.get("default", {}))
    for section, values in data.items():
        if section != "default" and car_class.lower().startswith(section.lower()):
            out.update(values)
    return out
