"""Wire protocol v1 between the phone and the driving service.

Text WebSocket messages are JSON objects tagged by ``type``. Binary messages
are camera frames: ``[uint32 LE header length][header JSON][JPEG bytes]``.
The contract is documented in docs/phone-protocol.md; keep the two in sync.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 1
MAX_IMU_SAMPLES = 500
MAX_LABEL_CHARS = 512
MAX_IMAGE_SIDE_PX = 8192
# Link stats bounds. Counters run per connection: 10^9 frames is months at 60 fps.
MAX_LINK_COUNT = 1_000_000_000
# The phone keeps at most 3 frames in flight once acks flow; before the first ack it
# tracks every send for up to 1 s, so a few hundred is generous.
MAX_LINK_IN_FLIGHT = 1000
MAX_ACK_RTT_MS = 600_000.0

_LENGTH_PREFIX = struct.Struct("<I")
_JPEG_START = b"\xff\xd8\xff"


class ProtocolError(ValueError):
    """A message from the phone did not match the protocol."""


class FrameError(ProtocolError):
    """A binary frame was refused. ``seq`` is set when its header could be read."""

    def __init__(self, message: str, seq: int | None = None) -> None:
        super().__init__(message)
        self.seq = seq


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class _PhoneMessage(_Wire):
    v: Literal[1]
    # Phone clock, milliseconds since the Unix epoch, when the reading was taken.
    t: float = Field(gt=0)


class CameraInfo(_Wire):
    label: str = Field(max_length=MAX_LABEL_CHARS)
    width: int = Field(gt=0, le=MAX_IMAGE_SIDE_PX)
    height: int = Field(gt=0, le=MAX_IMAGE_SIDE_PX)
    fps: float = Field(gt=0, le=240)


class MountInfo(_Wire):
    height_m: float = Field(gt=0, le=3)
    pitch_deg: float = Field(ge=-90, le=90)


class Hello(_PhoneMessage):
    type: Literal["hello"]
    user_agent: str = Field(max_length=MAX_LABEL_CHARS)
    camera: CameraInfo
    mount: MountInfo | None = None
    # "device_xyz": imu rx, ry, rz are rotation about device x, y, z. Absent on
    # older pages, which wrote [about y, about z, about x] in those columns.
    imu_axes: Literal["device_xyz"] | None = None


class Gps(_PhoneMessage):
    type: Literal["gps"]
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0)
    altitude_m: float | None = None
    speed_mps: float | None = Field(default=None, ge=0)
    # Direction of travel, degrees clockwise from true north. Absent when stopped.
    course_deg: float | None = Field(default=None, ge=0, lt=360)
    # Time the fix itself was computed, which can lag ``t``.
    fix_t: float = Field(gt=0)


class Heading(_PhoneMessage):
    type: Literal["heading"]
    # Degrees clockwise from magnetic north.
    magnetic_deg: float = Field(ge=0, lt=360)
    accuracy_deg: float | None = None


# [ms since message t, accel x, y, z (m/s^2, with gravity), rotation rate x, y, z (deg/s)]
ImuSample = tuple[float, float, float, float, float, float, float]


class Imu(_PhoneMessage):
    type: Literal["imu"]
    samples: tuple[ImuSample, ...] = Field(min_length=1, max_length=MAX_IMU_SAMPLES)


class Ping(_PhoneMessage):
    type: Literal["ping"]
    # The phone's measurement from its previous ping/pong exchange, if it has one.
    rtt_ms: float | None = Field(default=None, ge=0)
    offset_ms: float | None = None


# Strict: JSON true/false or "5" must not pass as a count.
LinkCount = Annotated[int, Field(ge=0, le=MAX_LINK_COUNT, strict=True)]


class Link(_PhoneMessage):
    """How the phone's uplink is coping, once a second. Counters run per connection."""

    type: Literal["link"]
    captured: LinkCount
    sent: LinkCount
    acked: LinkCount
    skipped_flow: LinkCount
    skipped_encoder: LinkCount
    skipped_socket: LinkCount
    in_flight: int = Field(ge=0, le=MAX_LINK_IN_FLIGHT, strict=True)
    jpeg_quality: float = Field(gt=0, le=1)
    # Smoothed send -> ack time; null until the first ack.
    ack_rtt_ms: float | None = Field(default=None, ge=0, le=MAX_ACK_RTT_MS)


type TextMessage = Hello | Gps | Heading | Imu | Ping | Link

PhoneMessage = Annotated[Hello | Gps | Heading | Imu | Ping | Link, Field(discriminator="type")]
_phone_message = TypeAdapter(PhoneMessage)


class Pong(_Wire):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["pong"] = "pong"
    t: float
    server_t: float

    def to_wire(self) -> str:
        return self.model_dump_json()


class Ack(_Wire):
    """Sent after each binary frame the service handled, so the phone can pace itself."""

    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["ack"] = "ack"
    seq: int
    # Server wall clock, milliseconds since the Unix epoch, when the frame was handled.
    t: float

    def to_wire(self) -> str:
        return self.model_dump_json()


class ErrorReply(_Wire):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["error"] = "error"
    message: str

    def to_wire(self) -> str:
        return self.model_dump_json()


class FrameHeader(_Wire):
    # Counts every frame the phone captured, including ones it chose not to send.
    seq: int = Field(ge=0)
    t: float = Field(gt=0)
    w: int = Field(gt=0, le=MAX_IMAGE_SIDE_PX)
    h: int = Field(gt=0, le=MAX_IMAGE_SIDE_PX)
    # Clockwise degrees the phone turned the camera image so it is upright and
    # landscape when the phone is mounted sideways. Absent from older phone pages.
    rot: Literal[0, 90, 180, 270] | None = None


@dataclass(frozen=True, slots=True)
class Frame:
    header: FrameHeader
    jpeg: bytes


def _describe(error: ValidationError) -> str:
    first = error.errors(include_url=False, include_input=False)[0]
    location = ".".join(str(part) for part in first["loc"]) or "message"
    return f"{location}: {first['msg']}"


def parse_text(raw: str, max_bytes: int) -> TextMessage:
    """Validate one JSON message from the phone."""
    if len(raw) > max_bytes:
        raise ProtocolError(f"message too large ({len(raw)} > {max_bytes})")
    try:
        return _phone_message.validate_json(raw)
    except ValidationError as error:
        if any(item["type"] == "json_invalid" for item in error.errors()):
            raise ProtocolError("message is not valid JSON") from error
        raise ProtocolError(_describe(error)) from error


def _readable_seq(header_bytes: bytes) -> int | None:
    """The seq of a header that failed validation, if it is a JSON object with a usable one.

    Lets the service ack a frame refused for, say, ``w: 0`` or a field added by a
    newer page, so the phone does not hold an in-flight slot until its ack timeout.
    """
    try:
        decoded = json.loads(header_bytes)
    except (ValueError, RecursionError):
        return None
    if not isinstance(decoded, dict):
        return None
    seq = decoded.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return None
    return seq


def _parse_header(raw: bytes, max_header_bytes: int) -> tuple[FrameHeader, int]:
    """The frame header and where the JPEG starts.

    Raises FrameError, carrying the seq when the header is a JSON object with a
    usable seq but fails validation for another reason.
    """
    if len(raw) < _LENGTH_PREFIX.size:
        raise FrameError("frame too short to hold a header length")
    (header_length,) = _LENGTH_PREFIX.unpack_from(raw, 0)
    if header_length > max_header_bytes:
        raise FrameError(f"frame header too large ({header_length} > {max_header_bytes})")
    body_start = _LENGTH_PREFIX.size + header_length
    if body_start > len(raw):
        raise FrameError("frame header runs past the end of the message")
    header_bytes = raw[_LENGTH_PREFIX.size : body_start]
    try:
        header = FrameHeader.model_validate_json(header_bytes)
    except ValidationError as error:
        raise FrameError(
            f"frame header invalid: {_describe(error)}", seq=_readable_seq(header_bytes)
        ) from error
    return header, body_start


def _peek_seq(raw: bytes, max_header_bytes: int) -> int | None:
    """The seq of a frame refused for its size, if its (bounded) header is readable."""
    try:
        header, _ = _parse_header(raw, max_header_bytes)
    except FrameError as error:
        return error.seq
    return header.seq


def parse_frame(raw: bytes, max_bytes: int, max_header_bytes: int) -> Frame:
    """Validate one binary camera frame from the phone.

    Raises FrameError; its ``seq`` is set whenever the header was readable, so the
    service can still ack a frame it refused.
    """
    if len(raw) > max_bytes:
        seq = _peek_seq(raw, max_header_bytes)
        raise FrameError(f"frame too large ({len(raw)} > {max_bytes})", seq=seq)
    header, body_start = _parse_header(raw, max_header_bytes)
    jpeg = raw[body_start:]
    if not jpeg.startswith(_JPEG_START):
        raise FrameError("frame payload is not a JPEG image", seq=header.seq)
    return Frame(header=header, jpeg=jpeg)


def encode_frame(header: FrameHeader, jpeg: bytes) -> bytes:
    """Build the binary form of a frame. Used by the fake phone and tests."""
    header_bytes = json.dumps(header.model_dump(exclude_none=True)).encode()
    return _LENGTH_PREFIX.pack(len(header_bytes)) + header_bytes + jpeg
