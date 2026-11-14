"""Sliding one-second rate limit as a pure function over an immutable window."""

from __future__ import annotations

from dataclasses import dataclass

WINDOW_S = 1.0


@dataclass(frozen=True, slots=True)
class RateWindow:
    times: tuple[float, ...] = ()


def admit(window: RateWindow, now: float, limit_per_s: int) -> tuple[bool, RateWindow]:
    """Decide whether an event at ``now`` is within the limit.

    Returns the decision and the window to use for the next event.
    """
    recent = tuple(t for t in window.times if now - t < WINDOW_S)
    if len(recent) >= limit_per_s:
        return False, RateWindow(times=recent)
    return True, RateWindow(times=(*recent, now))
