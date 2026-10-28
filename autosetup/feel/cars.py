"""Which car a recording is.

A livery name ('Iron Dames 2025 #85') is one entry of a car; the car signature (its
parameter ranges) changes with game updates. What you like is about the car itself, so
it's keyed by the car model in the saved setups' VehicleClassSetting, e.g.
'GT3 Porsche_911_GT3_R_LMGT3 WEC2025' → 'Porsche_911_GT3_R_LMGT3' (seasons and series
labels dropped). Until a recording of the livery has been matched to a saved setup, the
livery name stands in.
"""

from __future__ import annotations

import re

_SEASON = re.compile(r"^(WEC|ELMS|IMSA)\d{4}$")
_CLASSES = {"gt3", "gte", "lmgt3", "lmp2", "lmp2_elms", "lmp3", "hypercar", "hyper"}


def model_of(vehicle_class: str, car_class: str = "") -> str | None:
    """'ELMS2026 LMP2_ELMS Oreca_07' → 'Oreca_07'."""
    words = [w for w in vehicle_class.split() if not _SEASON.match(w)
             and w.lower() not in _CLASSES and w.lower() != car_class.lower()]
    named = [w for w in words if "_" in w] or words
    return named[0] if named else None


def car_key(store, car_name: str, car_class: str = "") -> str:
    row = store.con.execute("select vehicle_class from car_classes where car_name = ?", [car_name]).fetchone()
    model = model_of(row[0], car_class) if row and row[0] else None
    return model or car_name


def recording_car(store, rid: int) -> tuple[str, str]:
    """(car key, car class) of a recording."""
    car_name, car_class = store.con.execute("select car_name, car_class from recordings where id = ?", [rid]).fetchone()
    return car_key(store, car_name, car_class or ""), car_class or ""
