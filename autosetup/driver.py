"""The Driver screen: where your locked fingerprints sit, per car class.

Each car with a fingerprint is a point on two axes, in button steps against the class's
usual balance (the median of every current-build stint of the class): Looser ↔ Safer
(mid-corner and exits) and Pointier ↔ Lazier (entries), with one line about it
("Pointy entry, stable exit, slightly more rake than usual").
"""

from __future__ import annotations

import numpy as np

from autosetup.feel import fingerprint as fpm
from autosetup.knowledge import load_knowledge
from autosetup.lmu.builds import current_filter


def _usual_rake(store, car_class: str) -> float | None:
    cond, params = current_filter(store)
    vals = [r for (r,) in store.con.execute(
        f"""select rh_rear_fast - rh_front_fast from recordings r where car_class = ? and {cond}
            and rh_front_fast is not null and rh_rear_fast is not null""", [car_class, *params]).fetchall()]
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
            usual = fpm.usual(store, car_class)
            rake = _usual_rake(store, car_class)
            for fp in fps:
                rear, front = fpm.axes(fp, usual, step)
                cars.append({"car_key": fp.car_key, "rear": rear, "front": front, "layout": fp.layout,
                             "created_at": fp.created_at, "line": fpm.summary(fp, usual, step, rake),
                             "fingerprint": fp.to_json()})
        out.append({"car_class": car_class, "laps": int(laps), "cars": cars})
    return {"classes": out}
