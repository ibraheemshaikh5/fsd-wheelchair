"""Chair wire messages (docs/chair-link.md): hello and chair status in, drive out.

Every message is a frozen model that refuses unknown fields, NaN and infinity,
and values outside sane bounds. Anything the board sends is checked here before
the service looks at it.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from drive_service.chair.config import MAX_TTL_MS

MAX_FIRMWARE_CHARS = 64
# Sanity bounds on what a board may claim; its real caps are far lower.
MAX_BOARD_SPEED_MPS = 3.0
MAX_BOARD_TURN_RADPS = 3.0
MAX_ERROR_DETAILS = 3

StopReason = Literal["watchdog", "estop", "joystick", "no_link", "limit"]


class ChairProtocolError(ValueError):
    """A message from the board that the service refuses."""


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ChairHello(_Message):
    type: Literal["hello"]
    role: Literal["chair"]
    firmware: str = Field(min_length=1, max_length=MAX_FIRMWARE_CHARS)
    max_speed_mps: float = Field(ge=0, le=MAX_BOARD_SPEED_MPS)
    max_turn_radps: float = Field(ge=0, le=MAX_BOARD_TURN_RADPS)


class ChairOutput(_Message):
    v_mps: float = Field(ge=0, le=MAX_BOARD_SPEED_MPS)
    w_radps: float = Field(ge=-MAX_BOARD_TURN_RADPS, le=MAX_BOARD_TURN_RADPS)


class ChairStatus(_Message):
    type: Literal["chair"]
    t: float = Field(ge=0)
    mode: Literal["auto", "manual"]
    joystick_active: bool
    estop: bool
    battery_pct: float | None = Field(ge=0, le=100)
    applied_seq: int | None = Field(ge=0)
    output: ChairOutput
    stopped_reason: StopReason | None


class DriveCommand(_Message):
    type: Literal["drive"] = "drive"
    seq: int = Field(ge=0)
    t: int = Field(ge=0)
    v_mps: float = Field(ge=0)
    w_radps: float
    ttl_ms: int = Field(ge=1, le=MAX_TTL_MS)

    def to_wire(self) -> str:
        return self.model_dump_json()


type ChairMessage = ChairHello | ChairStatus

_CHAIR_MESSAGE = TypeAdapter(
    Annotated[ChairHello | ChairStatus, Field(discriminator="type")],
)


def _describe(error: ValidationError) -> str:
    details = [
        f"{'.'.join(str(part) for part in item['loc']) or 'message'}: {item['msg']}"
        for item in error.errors()[:MAX_ERROR_DETAILS]
    ]
    return "; ".join(details)


def parse_chair_message(text: str, max_bytes: int) -> ChairMessage:
    """Validate one text message from the board, or raise ChairProtocolError."""
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise ChairProtocolError(f"message is {size} bytes; the limit is {max_bytes}")
    try:
        # Parsed by json first so the error for "not JSON" is plain, and NaN is caught below.
        raw = json.loads(text)
    except ValueError as error:
        raise ChairProtocolError("message is not JSON") from error
    if not isinstance(raw, dict):
        raise ChairProtocolError("message must be a JSON object")
    try:
        return _CHAIR_MESSAGE.validate_json(text)
    except ValidationError as error:
        raise ChairProtocolError(f"invalid chair message: {_describe(error)}") from error
