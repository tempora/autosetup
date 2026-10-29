"""Setup file names and garage descriptions from templates (Settings → Setup files).

    {track} {car} {class} {date} {time} {n} {stint} {lap} {feel} {changes}

``{car}`` is the car model ("Aston Martin Vantage"), never the team; until the car has been
matched to a saved setup it isn't known and is left out with its separator. Unknown
fields stay as typed.
"""

from __future__ import annotations

import re

NAME = "{track} {car} {date} {n}"
NOTES = "From stint {stint} ({lap}), {feel}. {changes}"

MODELS = {"AMR_LMGT3": "Aston Martin Vantage", "BMW_M4_LMGT3": "BMW M4",
          "Porsche_911_GT3_R_LMGT3": "Porsche 911 GT3 R", "Oreca_07": "Oreca 07"}
CLASS_TAGS = {"lmgt3", "gt3", "lmp2", "lmp2_elms", "lmh", "lmdh", "hy", "hypercar", "gte", "lmgte", "lmp3"}
_FIELD = re.compile(r"\{(\w+)\}")
_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def car_name(model: str | None) -> str:
    """'AMR_LMGT3' → 'Aston Martin Vantage'; otherwise underscores to spaces, class tags dropped."""
    if not model:
        return ""
    if model in MODELS:
        return MODELS[model]
    return " ".join(w for w in model.split("_") if w and w.lower() not in CLASS_TAGS)


def render(template: str, fields: dict[str, object]) -> str:
    """Fill the fields in. One that's known but empty goes with the separator before it (or
    after it, at the start); unknown ones stay as typed."""
    def blank(name: str) -> bool:
        return name in fields and (fields[name] is None or str(fields[name]) == "")

    out = re.sub(r"[ _-]*\{(\w+)\}", lambda m: m.group(0) if not blank(m.group(1)) else "", template)
    out = re.sub(r"^\{(\w+)\}[ _-]*", lambda m: m.group(0) if not blank(m.group(1)) else "", out)
    out = _FIELD.sub(lambda m: str(fields[m.group(1)]) if m.group(1) in fields else m.group(0), out)
    return out.strip()


def file_safe(name: str) -> str:
    """A name Windows takes as a file name: no \\ / : * ? " < > |, no dots or spaces at the ends."""
    return _UNSAFE.sub("-", name).strip(" .")


def lap_text(seconds: float | None, for_file: bool) -> str:
    """1:43.46 (description) or 1-43.46 (file name)."""
    if seconds is None:
        return ""
    m, s = divmod(seconds, 60.0)
    return f"{int(m)}{'-' if for_file else ':'}{s:05.2f}"


def feel_text(front: int, rear: int) -> str:
    parts = [p for p in ({-1: "Pointier", 1: "Lazier"}.get(front), {-1: "Looser", 1: "Safer"}.get(rear)) if p]
    return " + ".join(parts) or "As it was"
