"""Shared test fixtures: a controllable clock, a no-op recorder, and message builders."""

from __future__ import annotations

import json
from typing import Any

from drive_service.protocol import Frame, FrameHeader, encode_frame

# Smallest byte string the service accepts as a JPEG: start marker, filler, end marker.
TINY_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"


class FakeTime:
    """Time source the test advances by hand."""

    def __init__(self, mono: float = 100.0, wall_ms: float = 1_700_000_000_000.0) -> None:
        self._mono = mono
        self._wall_ms = wall_ms

    def mono(self) -> float:
        return self._mono

    def wall_ms(self) -> float:
        return self._wall_ms

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._wall_ms += seconds * 1000.0


class NullRecorder:
    """Recorder sink that drops everything."""

    def write_frame(self, frame, received_wall_ms):
        pass

    def write_sensor(self, message, received_wall_ms):
        pass

    def write_derived(self, record, received_wall_ms):
        pass


def gps_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "v": 1,
        "type": "gps",
        "t": 1_700_000_000_000.0,
        "lat": 38.9869,
        "lon": -76.9426,
        "accuracy_m": 4.5,
        "altitude_m": 40.0,
        "speed_mps": 1.2,
        "course_deg": 90.0,
        "fix_t": 1_700_000_000_000.0,
    }
    return {**base, **overrides}


def heading_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "v": 1,
        "type": "heading",
        "t": 1_700_000_000_000.0,
        "magnetic_deg": 45.0,
        "accuracy_deg": 10.0,
    }
    return {**base, **overrides}


def hello_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "v": 1,
        "type": "hello",
        "t": 1_700_000_000_000.0,
        "user_agent": "test-phone",
        "camera": {"label": "Back Ultra Wide Camera", "width": 640, "height": 480, "fps": 10.0},
        "mount": {"height_m": 1.1, "pitch_deg": -10.0},
    }
    return {**base, **overrides}


def imu_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "v": 1,
        "type": "imu",
        "t": 1_700_000_000_000.0,
        "samples": [[0.0, 0.1, 9.8, 0.2, 1.0, 2.0, 3.0], [16.0, 0.1, 9.8, 0.2, 1.0, 2.0, 3.0]],
    }
    return {**base, **overrides}


def ping_dict(**overrides: Any) -> dict[str, Any]:
    base = {"v": 1, "type": "ping", "t": 1_700_000_000_000.0}
    return {**base, **overrides}


def link_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "v": 1,
        "type": "link",
        "t": 1_700_000_000_000.0,
        "captured": 600,
        "sent": 240,
        "acked": 236,
        "skipped_flow": 300,
        "skipped_encoder": 40,
        "skipped_socket": 20,
        "in_flight": 3,
        "jpeg_quality": 0.5,
        "ack_rtt_ms": 180.0,
    }
    return {**base, **overrides}


def as_json(message: dict[str, Any]) -> str:
    return json.dumps(message)


def make_frame(seq: int = 0, t: float = 1_700_000_000_000.0, jpeg: bytes = TINY_JPEG) -> Frame:
    return Frame(header=FrameHeader(seq=seq, t=t, w=640, h=480), jpeg=jpeg)


def frame_bytes(seq: int = 0, t: float = 1_700_000_000_000.0, jpeg: bytes = TINY_JPEG) -> bytes:
    return encode_frame(FrameHeader(seq=seq, t=t, w=640, h=480), jpeg)
