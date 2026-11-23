"""Telemetry model for a simulated industrial press with gradual wear."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import ClassVar

MIN_INTERVAL_MS = 1_000
MAX_INTERVAL_MS = 3_600_000
HEALTH_BANDS: tuple[tuple[float, str], ...] = ((0.4, "healthy"), (0.7, "degraded"))


@dataclass
class MachineState:
    interval_ms: int = 60_000
    wear: float = 0.0


class MachineSimulator:
    """Produces correlated telemetry that degrades as wear accumulates.

    ``wear_rate`` multiplies how fast wear builds up; a degrading press uses a high
    rate so it crosses the alert thresholds within minutes instead of hours.
    """

    BASELINE: ClassVar[dict[str, float]] = {
        "temperature_c": 65.0,
        "vibration_mm_s": 2.5,
        "motor_current_a": 12.0,
        "rpm": 1500.0,
    }
    WEAR_PER_STEP = 0.002

    def __init__(self, state: MachineState, rng: random.Random | None = None, wear_rate: float = 1.0) -> None:
        if wear_rate < 0:
            raise ValueError("wear_rate must not be negative")
        self._state = state
        self._rng = rng or random.Random()
        self._wear_rate = wear_rate

    @property
    def state(self) -> MachineState:
        return self._state

    def step(self) -> None:
        increment = self._rng.uniform(0.0, self.WEAR_PER_STEP) * self._wear_rate
        self._state.wear = min(1.0, self._state.wear + increment)

    def read(self) -> dict[str, float]:
        wear, noise = self._state.wear, self._rng.gauss
        return {
            "temperature_c": round(self.BASELINE["temperature_c"] + wear * 25 + noise(0, 0.6), 2),
            "vibration_mm_s": round(self.BASELINE["vibration_mm_s"] + wear * 6 + noise(0, 0.15), 3),
            "motor_current_a": round(self.BASELINE["motor_current_a"] + wear * 4 + noise(0, 0.3), 2),
            "rpm": round(self.BASELINE["rpm"] - wear * 120 + noise(0, 10), 1),
        }

    def health(self) -> str:
        for upper, label in HEALTH_BANDS:
            if self._state.wear < upper:
                return label
        return "critical"

    def reset_wear(self) -> None:
        self._state.wear = 0.0
