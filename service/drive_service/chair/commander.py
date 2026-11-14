"""Decide the next drive command. Pure: no clock, no I/O.

The rule is stop unless a bench test command is running, and never past the
caps. Stopping is a command too, so a command comes out on every call.
"""

from __future__ import annotations

from dataclasses import dataclass

from drive_service.chair.config import MS_PER_S, ChairSettings
from drive_service.chair.protocol import ChairHello, DriveCommand

# Who asked for the motion: a bench test, or the driving model.
SOURCE_TEST = "test"
SOURCE_MODEL = "model"

STOP_V_MPS = 0.0
STOP_W_RADPS = 0.0


@dataclass(frozen=True, slots=True)
class Caps:
    max_speed_mps: float
    max_turn_radps: float


@dataclass(frozen=True, slots=True)
class BenchCommand:
    """Motion valid until ``expires_mono``: a bench test (POST /api/chair/test) or
    the model's latest decision (POST /api/chair/drive)."""

    v_mps: float
    w_radps: float
    expires_mono: float
    source: str = SOURCE_TEST


def bench_active(bench: BenchCommand | None, now_mono: float) -> bool:
    return bench is not None and now_mono < bench.expires_mono


def effective_caps(settings: ChairSettings, hello: ChairHello | None) -> Caps:
    """The service's bench caps, lowered to the board's own caps once it has said hello."""
    if hello is None:
        return Caps(settings.max_test_speed_mps, settings.max_test_turn_radps)
    return Caps(
        min(settings.max_test_speed_mps, hello.max_speed_mps),
        min(settings.max_test_turn_radps, hello.max_turn_radps),
    )


def command_lag_ms(last_seq: int | None, applied_seq: int | None, rate_hz: float) -> float:
    """How far behind the newest command the board's applied one is, in ms.

    Zero when the board applies nothing (it is stopped, so nothing old is driving)
    or reports a seq the service has not sent (left over from before a restart).
    """
    if last_seq is None or applied_seq is None or applied_seq > last_seq:
        return 0.0
    return (last_seq - applied_seq) * MS_PER_S / rate_hz


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def next_command(
    *,
    now_mono: float,
    wall_ms: float,
    bench: BenchCommand | None,
    last_seq: int | None,
    caps: Caps,
    ttl_ms: int,
) -> DriveCommand:
    seq = 0 if last_seq is None else last_seq + 1
    v_mps, w_radps = STOP_V_MPS, STOP_W_RADPS
    if bench is not None and bench_active(bench, now_mono):
        v_mps = _clamp(bench.v_mps, 0.0, caps.max_speed_mps)
        w_radps = _clamp(bench.w_radps, -caps.max_turn_radps, caps.max_turn_radps)
    return DriveCommand(seq=seq, t=int(wall_ms), v_mps=v_mps, w_radps=w_radps, ttl_ms=ttl_ms)
