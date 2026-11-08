"""The Driver screen: where your locked fingerprints sit, per car class.

Each car with a fingerprint sits on the three feel sliders (front, rear, fast-corner
rear), in degrees against that car's usual balance (the median of every stint of the
car: cars differ in more than setup, so a car reads against itself), with one line about it ("Pointy front, safe rear, slightly more rake than usual").
``span`` is the sliders' end, so the Driver screen draws them to the same scale.
"""

from __future__ import annotations

import numpy as np

from autosetup.feel import fingerprint as fpm
from autosetup.feel.target import RANGE_STEPS
from autosetup.knowledge import load_knowledge

def _usual_rake(store, rids: list[int]) -> float | None:
    vals = [r for (r,) in store.con.execute(
        """select rh_rear_fast - rh_front_fast from recordings r where id in (select unnest(?::INTEGER[]))
           and rh_front_fast is not null and rh_rear_fast is not null""", [rids]).fetchall()] if rids else []
    return float(np.median(vals)) if vals else None


def profile(store) -> dict:
    classes = store.con.execute(
        """select car_class, max(file_mtime) from recordings where car_class is not null and duration > 30
           group by car_class order by 2 desc""").fetchall()
    out = []
    for (car_class, _) in classes:
        fps = fpm.of_class(store, car_class)
        laps = store.con.execute(
            """select count(*) from laps l join recordings r on r.id = l.recording_id
               where r.car_class = ? and l.complete and not l.in_pits""", [car_class]).fetchone()[0]
        if not laps:
            continue
        step = float(load_knowledge(car_class)["feel_step_deg"])
        cars = []
        if fps:
            for fp in fps:
                usual = fpm.usual(store, car_class, fp.car_key)
                rake = _usual_rake(store, fpm.car_stints(store, car_class, fp.car_key))
                cars.append({"car_key": fp.car_key, **fpm.axes(fp, usual), "layout": fp.layout,
                             "created_at": fp.created_at, "line": fpm.summary(fp, usual, step, rake),
                             "fingerprint": fp.to_json()})
        out.append({"car_class": car_class, "laps": int(laps), "span": RANGE_STEPS * step, "step": step, "cars": cars})
    return {"classes": out}
