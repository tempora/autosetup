"""What the aero platform is worth at this layout: d(lap time)/d(rear wing) and
d(lap time)/d(rake), from the lap model and the car's aero map.

The wing comes first (the drag level), and the balance is then solved with rake, ride
heights, bars, springs and diff around it, so these are what the optimiser starts from.
"""

from __future__ import annotations

from dataclasses import dataclass

from autosetup.sim.aero import AeroMap, Platform
from autosetup.sim.model import LayoutModel

RAKE_STEP_MM = 1.0


@dataclass(frozen=True)
class Sensitivity:
    wing_s_per_click: float  # s, one rear-wing click up (+ = slower)
    rake_s_per_mm: float  # s, one mm more rake at speed (rear up)
    lap_time: float  # s, simulated at the platform asked about


def aero_sensitivity(model: LayoutModel, aero: AeroMap, platform: Platform, wing_clicks: int) -> Sensitivity:
    """``wing_clicks``: positions of the rear wing (a click is 1 / (clicks − 1) of its range)."""
    step = 1.0 / max(wing_clicks - 1, 1)

    def lap(p: Platform) -> float:
        return model.simulate(model.car_at(aero, p)).time

    base = lap(platform)
    up = lap(Platform(platform.wing + step, platform.rh_front, platform.rh_rear))
    down = lap(Platform(platform.wing - step, platform.rh_front, platform.rh_rear))
    rake_up = lap(Platform(platform.wing, platform.rh_front, platform.rh_rear + RAKE_STEP_MM))
    rake_down = lap(Platform(platform.wing, platform.rh_front, platform.rh_rear - RAKE_STEP_MM))
    return Sensitivity((up - down) / 2, (rake_up - rake_down) / (2 * RAKE_STEP_MM), base)
