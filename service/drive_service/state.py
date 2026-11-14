"""The latest reading from each phone sensor, as an immutable snapshot.

Anything that drives the chair must check freshness with :func:`is_fresh`
before trusting a reading.

A reading's age runs from the OLDER of two moments: when the service received
it, and when the phone captured it (the phone's timestamp converted to this
machine's clock through the ping/pong offset, see ``Hub``). Taking the older
one means transit delay, a backlogged link or a resent fix makes a reading look
staler, and a wrong offset or a jump in the phone's clock can never make it look
fresher than its arrival. Freshness fails closed: before the clocks are synced a
reading has no capture time and is never fresh.

``is_fresh`` judges one reading. If the hub drops its clock estimate (a reading
claimed to come from the future), readings stored before that keep their capture
time until they age out, so a drive loop must also require the phone to be
connected with a synced clock (``status.clock_synced``, or
``clock.is_synced(snapshot.clock, ...)`` together with ``phone_connected``).
"""

from __future__ import annotations

from dataclasses import dataclass

from drive_service.clock import ClockEstimate
from drive_service.motion import MotionEstimate
from drive_service.protocol import Frame, Gps, Heading, Hello, Link

MS_PER_S = 1000.0


@dataclass(frozen=True, slots=True)
class Stamped[T]:
    value: T
    received_mono: float
    # Service monotonic time the reading was captured, never later than
    # received_mono. None when the clocks were not synced at receipt.
    captured_mono: float | None = None


@dataclass(frozen=True, slots=True)
class SensorSnapshot:
    hello: Hello | None = None
    frame: Stamped[Frame] | None = None
    gps: Stamped[Gps] | None = None
    heading: Stamped[Heading] | None = None
    # Derived from IMU and GPS; aged by the newest IMU sample it used.
    motion: Stamped[MotionEstimate] | None = None
    clock: ClockEstimate | None = None
    # The phone's own uplink report; aged by receive time only (captured_mono None).
    link: Stamped[Link] | None = None
    phone_connected: bool = False


def age_ms(reading: Stamped | None, now_mono: float) -> float | None:
    """Age from capture when known, else from receipt (for display only)."""
    if reading is None:
        return None
    since = reading.received_mono if reading.captured_mono is None else reading.captured_mono
    return (now_mono - since) * MS_PER_S


def is_fresh(reading: Stamped | None, max_age_ms: float, now_mono: float) -> bool:
    """True only for a reading with a known capture time no older than ``max_age_ms``."""
    if reading is None or reading.captured_mono is None:
        return False
    age = age_ms(reading, now_mono)
    return age is not None and age <= max_age_ms
