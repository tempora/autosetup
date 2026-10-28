"""Platform safety: the car mustn't bottom or run below the class's minimum ride height.

From the stint the proposal starts from: its lowest ride heights (1st percentile per
wheel) and its bottoming. A ride-height change moves the lowest point by the change
(mm per click from the car's labels). A car that already bottoms may not come down.
"""

from __future__ import annotations

from dataclasses import dataclass

from autosetup.opt.space import Dim

DEFAULT_MM_PER_CLICK = 1.0


@dataclass(frozen=True)
class Platform:
    rh_min: dict[str, float]  # "front" / "rear" → lowest ride height measured, mm
    bottoming_s: float
    min_mm: float
    bottoming_limit_s: float
    mm_per_click: dict[str, float]  # dim name → mm per index step (signed)

    def ok(self, base: dict[str, int], cand: dict[str, int]) -> bool:
        for axle in ("front", "rear"):
            name = f"rh_{axle}"
            if name not in cand or axle not in self.rh_min:
                continue
            delta = (cand[name] - base[name]) * self.mm_per_click.get(name, DEFAULT_MM_PER_CLICK)
            if delta < 0 and self.bottoming_s > self.bottoming_limit_s:
                return False
            if delta < 0 and self.rh_min[axle] + delta < self.min_mm:
                return False
        return True


def platform_of(summary: dict, dims: list[Dim], knowledge: dict, scales=None) -> Platform:
    rh = {}
    for axle, wheels in (("front", ("fl", "fr")), ("rear", ("rl", "rr"))):
        vals = [summary.get(f"rh_min_{w}") for w in wheels if summary.get(f"rh_min_{w}") is not None]
        if vals:
            rh[axle] = min(vals)
    per = {}
    for d in dims:
        if d.name.startswith("rh_"):
            step = scales[d.keys[0]].per_step if scales is not None else None
            per[d.name] = (step * 10.0) if step else DEFAULT_MM_PER_CLICK  # labels are cm
    return Platform(rh, float(summary.get("bottoming_s") or 0.0), float(knowledge["min_ride_height_mm"]),
                    float(knowledge["bottoming_s"]), per)
