"""Time sources, injectable so tests can control the clock."""

from __future__ import annotations

import time
from typing import Protocol


class TimeSource(Protocol):
    def mono(self) -> float:
        """Seconds on a clock that never jumps. Use for ages and rates."""
        ...

    def wall_ms(self) -> float:
        """Milliseconds since the Unix epoch. Use for comparing with the phone."""
        ...


class SystemTime:
    def mono(self) -> float:
        return time.monotonic()

    def wall_ms(self) -> float:
        return time.time() * 1000.0
