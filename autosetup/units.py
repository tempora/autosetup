"""Display units for the numbers autosetup writes itself (reasons, tables). The game's own
setting labels ("172 kPa", "-2.2 deg") are shown as LMU shows them in the garage.

``current`` is set by the server from your settings; everything else reads it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

CHOICES = {"speed": ("km/h", "mph"), "temp": ("°C", "°F"), "pressure": ("kPa", "psi")}


@dataclass
class Units:
    speed: str = "km/h"
    temp: str = "°C"
    pressure: str = "kPa"

    @classmethod
    def from_json(cls, data: dict | None) -> "Units":
        data = data or {}
        return cls(**{k: data[k] if data.get(k) in CHOICES[k] else CHOICES[k][0] for k in CHOICES})

    def to_json(self) -> dict:
        return asdict(self)

    def temp_value(self, c: float, delta: bool = False) -> float:
        """°C (or a difference in °C) in the chosen unit."""
        if self.temp == "°F":
            return c * 9 / 5 if delta else c * 9 / 5 + 32
        return c

    def fmt_temp(self, c: float, dec: int = 1, delta: bool = False, sign: bool = False) -> str:
        return f"{self.temp_value(c, delta):{'+' if sign else ''}.{dec}f} {self.temp}"

    def temp_range(self, lo: float, hi: float, delta: bool = False) -> str:
        """'5–12' in the chosen unit (no unit: the sentence carries it)."""
        return f"{self.temp_value(lo, delta):g}–{self.temp_value(hi, delta):g}" if self.temp == "°C" else \
            f"{self.temp_value(lo, delta):.0f}–{self.temp_value(hi, delta):.0f}"

    def pressure_value(self, kpa: float) -> float:
        return kpa * 0.145038 if self.pressure == "psi" else kpa

    def fmt_pressure(self, kpa: float, dec: int = 0) -> str:
        return f"{self.pressure_value(kpa):.{dec + (1 if self.pressure == 'psi' else 0)}f} {self.pressure}"


current = Units()


def set_current(units: Units) -> None:
    global current
    current = units
