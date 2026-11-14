"""Wire protocol between the chair and the driving service (/ws/chair).

Service -> chair: {"type": "drive", "seq", "t", "v_mps", "w_radps", "ttl_ms"}
Chair -> service: {"type": "hello", ...} once, then {"type": "chair", ...} at
status_hz. The microcontroller reports its state as a compact int array that
is mapped to JSON here.
"""

import json
import math
from dataclasses import dataclass
from typing import Any

DEFAULT_TTL_MS = 200
MAX_TTL_MS_ACCEPTED = 60_000  # Sanity bound only; the sketch caps ttl at 500 ms.
MAX_SEQ = 2**31 - 1  # The sketch takes seq as a 32-bit signed int.
NO_SEQ = -1
MILLI = 1000.0
STATUS_LENGTH = 7
LIMITS_LENGTH = 3

# Index = reason code from chair_control.h (chair::Reason).
STOP_REASONS: tuple[str | None, ...] = (None, "watchdog", "estop", "joystick", "no_link", "limit")


class ProtocolError(ValueError):
    """A message from the service or the microcontroller is malformed."""


@dataclass(frozen=True)
class DriveCommand:
    seq: int
    v_mps: float
    w_radps: float
    ttl_ms: int


@dataclass(frozen=True)
class McuLimits:
    max_speed_mps: float
    max_turn_radps: float
    max_ttl_ms: int

    @staticmethod
    def from_array(raw: Any) -> "McuLimits":
        values = _int_array(raw, LIMITS_LENGTH, "limits")
        if any(v <= 0 for v in values):
            raise ProtocolError(f"limits must be positive: {values}")
        return McuLimits(values[0] / MILLI, values[1] / MILLI, values[2])


@dataclass(frozen=True)
class McuStatus:
    mode_auto: bool
    joystick_active: bool
    estop: bool
    applied_seq: int | None
    out_v_mps: float
    out_w_radps: float
    stopped_reason: str | None

    @staticmethod
    def from_array(raw: Any) -> "McuStatus":
        mode, joystick, estop, seq, v_mm, w_mrad, reason = _int_array(raw, STATUS_LENGTH, "status")
        if any(flag not in (0, 1) for flag in (mode, joystick, estop)):
            raise ProtocolError(f"status flags must be 0 or 1: {raw}")
        if seq < NO_SEQ:
            raise ProtocolError(f"status applied_seq out of range: {seq}")
        if not 0 <= reason < len(STOP_REASONS):
            raise ProtocolError(f"status reason code unknown: {reason}")
        return McuStatus(
            mode_auto=bool(mode),
            joystick_active=bool(joystick),
            estop=bool(estop),
            applied_seq=None if seq == NO_SEQ else seq,
            out_v_mps=v_mm / MILLI,
            out_w_radps=w_mrad / MILLI,
            stopped_reason=STOP_REASONS[reason],
        )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_array(raw: Any, length: int, what: str) -> list[int]:
    if not isinstance(raw, list | tuple) or len(raw) != length:
        raise ProtocolError(f"{what} must be an array of {length} ints, got {raw!r}")
    if not all(_is_int(v) for v in raw):
        raise ProtocolError(f"{what} must contain only ints, got {raw!r}")
    return list(raw)


def _finite_number(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ProtocolError(f"drive '{key}' must be a finite number, got {value!r}")
    return float(value)


def _bounded_int(data: dict[str, Any], key: str, low: int, high: int) -> int:
    value = data.get(key)
    if not _is_int(value) or not low <= value <= high:
        raise ProtocolError(f"drive '{key}' must be an int in [{low}, {high}], got {value!r}")
    return value


def _parse_drive(data: dict[str, Any]) -> DriveCommand:
    v_mps = _finite_number(data, "v_mps")
    if v_mps < 0:
        raise ProtocolError(f"drive 'v_mps' must not be negative, got {v_mps}")
    return DriveCommand(
        seq=_bounded_int(data, "seq", 0, MAX_SEQ),
        v_mps=v_mps,
        w_radps=_finite_number(data, "w_radps"),
        ttl_ms=_bounded_int(
            {"ttl_ms": data.get("ttl_ms", DEFAULT_TTL_MS)}, "ttl_ms", 1, MAX_TTL_MS_ACCEPTED
        ),
    )


def parse_server_message(raw: str | bytes) -> DriveCommand | None:
    """Returns a DriveCommand, or None for message types the chair ignores."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"message is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("message must be a JSON object")
    kind = data.get("type")
    if not isinstance(kind, str):
        raise ProtocolError("message has no 'type'")
    return _parse_drive(data) if kind == "drive" else None


def build_hello(firmware: str, limits: McuLimits) -> dict[str, Any]:
    return {
        "type": "hello",
        "role": "chair",
        "firmware": firmware,
        "max_speed_mps": limits.max_speed_mps,
        "max_turn_radps": limits.max_turn_radps,
    }


def build_chair_message(status: McuStatus, now_ms: int) -> dict[str, Any]:
    return {
        "type": "chair",
        "t": now_ms,
        "mode": "auto" if status.mode_auto else "manual",
        "joystick_active": status.joystick_active,
        "estop": status.estop,
        "battery_pct": None,  # Not wired yet.
        "applied_seq": status.applied_seq,
        "output": {"v_mps": status.out_v_mps, "w_radps": status.out_w_radps},
        "stopped_reason": status.stopped_reason,
    }
