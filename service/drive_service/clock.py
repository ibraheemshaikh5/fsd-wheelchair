"""Estimate the difference between the phone's clock and this machine's.

The phone pings, the service answers with its own time, and the phone reports
the round-trip time and offset it measured in its next ping. The sample with
the shortest round trip is the least distorted by network delay, so that one
is used. ``server_time = phone_time + offset_ms``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClockSample:
    rtt_ms: float
    offset_ms: float


@dataclass(frozen=True, slots=True)
class ClockEstimate:
    offset_ms: float
    rtt_ms: float
    samples: int


def add_sample(
    samples: tuple[ClockSample, ...], sample: ClockSample, window: int
) -> tuple[ClockSample, ...]:
    """Return the samples with ``sample`` appended, keeping the newest ``window``."""
    return (*samples, sample)[-window:]


def estimate(samples: tuple[ClockSample, ...]) -> ClockEstimate | None:
    if not samples:
        return None
    best = min(samples, key=lambda sample: sample.rtt_ms)
    return ClockEstimate(offset_ms=best.offset_ms, rtt_ms=best.rtt_ms, samples=len(samples))


def to_server_ms(phone_ms: float, clock: ClockEstimate) -> float:
    return phone_ms + clock.offset_ms


def is_synced(clock: ClockEstimate | None, max_rtt_ms: float) -> bool:
    """True when there is an estimate whose round trip is short enough to trust.

    A min-RTT offset can be wrong by up to half its round trip, so a long round
    trip makes capture times too uncertain to judge freshness by.
    """
    return clock is not None and clock.rtt_ms <= max_rtt_ms
