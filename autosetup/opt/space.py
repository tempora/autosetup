"""The setup space the optimiser works in: a few settings that move balance and lap time,
and the dampers as a block of their own. Everything else stays where it is (or is set by
the rules: tyres, brake cooling, brake pressure, platform safety).

Settings are in the game's own click indices and stay integers. Per-wheel settings are
tied per axle (both sides move together). Range limits are the car's.
"""

from __future__ import annotations

from dataclasses import dataclass

from autosetup.lmu.setup_space import CarSetup

AXLES = {"front": ("FL", "FR"), "rear": ("RL", "RR")}
NOT_HANDLING = ("FUEL", "VIRTUAL_ENERGY", "COMPOUND", "PITSTOP", "RADIATOR", "ENGINE_MIXTURE", "PRESSURE",
                "BRAKE_DUCTS", "CAMBER")


def _axle(base: str, axle: str) -> list[str]:
    return [f"{base}-W_{w}" for w in AXLES[axle]]


# Priority order: the first MAX_MAIN the car has make the main block.
MAIN = [
    ("wing", ["VM_REAR_WING"]),
    ("front_wing", ["VM_FRONT_WING"]),
    ("rh_rear", _axle("WM_RIDEHEIGHT", "rear")),
    ("rh_front", _axle("WM_RIDEHEIGHT", "front")),
    ("arb_front", ["VM_FRONT_ANTISWAY"]),
    ("arb_rear", ["VM_REAR_ANTISWAY"]),
    ("diff_preload", ["VM_DIFF_PRELOAD"]),
    ("diff_power", ["VM_DIFF_POWER"]),
    ("diff_coast", ["VM_DIFF_COAST"]),
    ("front_diff_power", ["VM_FRONT_DIFF_POWER"]),
    ("front_diff_coast", ["VM_FRONT_DIFF_COAST"]),
    ("front_diff_preload", ["VM_FRONT_DIFF_PRELOAD"]),
    ("spring_front", _axle("WM_SPRING", "front")),
    ("spring_rear", _axle("WM_SPRING", "rear")),
    ("third_spring_front", ["VM_FRONT_3RD_SPRING"]),
    ("third_spring_rear", ["VM_REAR_3RD_SPRING"]),
    ("toe_front", ["VM_FRONT_TOEIN"]),
    ("toe_rear", ["VM_REAR_TOEIN"]),
    ("brake_bias", ["VM_BRAKE_BALANCE"]),
    ("tender_front", _axle("WM_TENDERSPRING", "front")),
    ("tender_rear", _axle("WM_TENDERSPRING", "rear")),
]
MAX_MAIN = len(MAIN)  # settings a car doesn't have take no place
DAMPERS = [
    *((f"{part}_{axle}", _axle(f"WM_{key}", axle)) for axle in ("front", "rear")
      for part, key in (("slow_bump", "SLOWBUMP"), ("slow_rebound", "SLOWREBOUND"),
                        ("fast_bump", "FASTBUMP"), ("fast_rebound", "FASTREBOUND"))),
    *((f"third_{part}_{axle}", [f"VM_{axle.upper()}_3RD_{key}"]) for axle in ("front", "rear")
      for part, key in (("slow_bump", "SLOWBUMP"), ("slow_rebound", "SLOWREBOUND"),
                        ("fast_bump", "FASTBUMP"), ("fast_rebound", "FASTREBOUND"), ("packers", "PACKERS"))),
    *((f"packers_{axle}", _axle("WM_PACKERS", axle)) for axle in ("front", "rear")),
]


@dataclass(frozen=True)
class Dim:
    name: str
    keys: tuple[str, ...]
    lo: int  # inclusive
    hi: int  # inclusive
    span: tuple[int, int] | None = None  # the setting's whole range, when lo..hi is cut to a limit

    @property
    def clicks(self) -> int:
        return self.hi - self.lo + 1

    def index(self, setup: CarSetup) -> int:
        return int(round(sum(setup[k].value for k in self.keys) / len(self.keys)))

    def x(self, index: float) -> float:
        """Share of the setting's whole range, 0..1 (a limit doesn't change what an index is)."""
        lo, hi = self.span or (self.lo, self.hi)
        return (index - lo) / max(hi - lo, 1)

    def clamp(self, index: int) -> int:
        return max(self.lo, min(self.hi, int(index)))


def _dims(setup: CarSetup, table: list[tuple[str, list[str]]], limit: int | None = None,
          locks: set[str] = frozenset()) -> list[Dim]:
    out = []
    for name, keys in table:
        ps = [setup.params.get(k) for k in keys]
        if any(p is None or not p.adjustable for p in ps) or any(k in locks for k in keys):
            continue
        out.append(Dim(name, tuple(keys), ps[0].min, ps[0].max - 1))
        if limit is not None and len(out) == limit:
            break
    return out


def main_dims(setup: CarSetup, locks: set[str] = frozenset()) -> list[Dim]:
    return _dims(setup, MAIN, MAX_MAIN, locks)


def damper_dims(setup: CarSetup, locks: set[str] = frozenset()) -> list[Dim]:
    return _dims(setup, DAMPERS, None, locks)


def aero_wing(dims: list[Dim]) -> Dim | None:
    """The wing the aero map and the drag level go by: the rear wing, or the front wing on
    a car whose rear wing can't be adjusted."""
    return next((d for d in dims if d.name == "wing"), None) or next((d for d in dims if d.name == "front_wing"), None)


def values(dims: list[Dim], indices: dict[str, int]) -> dict[str, int]:
    """Setting key → index for a point of the space."""
    out = {}
    for d in dims:
        if d.name in indices:
            for k in d.keys:
                out[k] = d.clamp(indices[d.name])
    return out


def setup_fingerprint(setup: CarSetup) -> tuple:
    """What makes two setups the same car to drive (fuel, tyres, cooling aside)."""
    return tuple(sorted((p.key, p.value) for p in setup.adjustable() if not any(w in p.key for w in NOT_HANDLING)))
