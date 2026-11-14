"""Exponential reconnect backoff with jitter."""

from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_BASE_S = 0.25
DEFAULT_CAP_S = 5.0
MIN_JITTER = 0.5  # Delay is scaled by a factor in [MIN_JITTER, 1].
MAX_EXPONENT = 32  # Avoids huge floats for long outages; the cap applies anyway.


@dataclass(frozen=True)
class Backoff:
    base_s: float = DEFAULT_BASE_S
    cap_s: float = DEFAULT_CAP_S

    def __post_init__(self) -> None:
        if self.base_s <= 0 or self.cap_s < self.base_s:
            raise ValueError("backoff needs 0 < base_s <= cap_s")

    def delay(self, attempt: int, rand: Callable[[], float]) -> float:
        """Seconds to wait before reconnect attempt number `attempt` (0-based)."""
        ceiling = min(self.cap_s, self.base_s * 2 ** min(attempt, MAX_EXPONENT))
        return ceiling * (MIN_JITTER + (1.0 - MIN_JITTER) * rand())
