import json
import struct

import pytest

from drive_service.protocol import (
    FrameHeader,
    Gps,
    Heading,
    Hello,
    Imu,
    Ping,
    Pong,
    ProtocolError,
    encode_frame,
    parse_frame,
    parse_text,
)
from helpers import (
    TINY_JPEG,
    as_json,
    frame_bytes,
    gps_dict,
    heading_dict,
    hello_dict,
    imu_dict,
    ping_dict,
)

MAX_TEXT = 64_000
MAX_FRAME = 1_000_000
MAX_HEADER = 1024


def test_parses_each_message_type():
    assert isinstance(parse_text(as_json(gps_dict()), MAX_TEXT), Gps)
    assert isinstance(parse_text(as_json(heading_dict()), MAX_TEXT), Heading)
    assert isinstance(parse_text(as_json(hello_dict()), MAX_TEXT), Hello)
    assert isinstance(parse_text(as_json(imu_dict()), MAX_TEXT), Imu)
    assert isinstance(parse_text(as_json(ping_dict()), MAX_TEXT), Ping)


def test_gps_fields_survive_parsing():
    gps = parse_text(as_json(gps_dict(lat=10.5, course_deg=None, speed_mps=None)), MAX_TEXT)
    assert gps.lat == 10.5
    assert gps.course_deg is None
    assert gps.speed_mps is None


def test_hello_mount_is_optional():
    hello = parse_text(as_json(hello_dict(mount=None)), MAX_TEXT)
    assert hello.mount is None


def test_ping_may_carry_previous_clock_sample():
    ping = parse_text(as_json(ping_dict(rtt_ms=40.0, offset_ms=-12.5)), MAX_TEXT)
    assert ping.rtt_ms == 40.0
    assert ping.offset_ms == -12.5


def test_messages_are_immutable():
    gps = parse_text(as_json(gps_dict()), MAX_TEXT)
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises its own ValidationError
        gps.lat = 0.0


@pytest.mark.parametrize(
    "message",
    [
        gps_dict(type="teleport"),
        gps_dict(v=2),
        gps_dict(surprise=1),
        gps_dict(lat=91.0),
        gps_dict(lon=-181.0),
        gps_dict(accuracy_m=-1.0),
        gps_dict(course_deg=360.0),
        gps_dict(speed_mps=-0.1),
        gps_dict(t=0),
        heading_dict(magnetic_deg=400.0),
        hello_dict(user_agent="x" * 2000),
        imu_dict(samples=[[0.0, 1.0]]),
        imu_dict(samples=[]),
        {"v": 1, "t": 1.0},
    ],
)
def test_rejects_invalid_messages(message):
    with pytest.raises(ProtocolError):
        parse_text(as_json(message), MAX_TEXT)


def test_rejects_non_json_text():
    with pytest.raises(ProtocolError, match="not valid"):
        parse_text("{nope", MAX_TEXT)


def test_rejects_nan_values():
    raw = as_json(gps_dict()).replace("38.9869", "NaN")
    with pytest.raises(ProtocolError):
        parse_text(raw, MAX_TEXT)


def test_rejects_oversized_text():
    with pytest.raises(ProtocolError, match="too large"):
        parse_text(as_json(gps_dict()), max_bytes=10)


def test_rejects_too_many_imu_samples():
    sample = [0.0, 0.1, 9.8, 0.2, 1.0, 2.0, 3.0]
    with pytest.raises(ProtocolError):
        parse_text(as_json(imu_dict(samples=[sample] * 2000)), 10_000_000)


def test_frame_round_trip():
    raw = frame_bytes(seq=7, t=123456.0)
    frame = parse_frame(raw, MAX_FRAME, MAX_HEADER)
    assert frame.header == FrameHeader(seq=7, t=123456.0, w=640, h=480)
    assert frame.jpeg == TINY_JPEG


def test_frame_header_length_is_little_endian_uint32():
    raw = frame_bytes()
    (length,) = struct.unpack_from("<I", raw, 0)
    header = json.loads(raw[4 : 4 + length])
    assert header["seq"] == 0


def test_rejects_frame_shorter_than_length_prefix():
    with pytest.raises(ProtocolError, match="too short"):
        parse_frame(b"\x01\x02", MAX_FRAME, MAX_HEADER)


def test_rejects_oversized_frame():
    with pytest.raises(ProtocolError, match="too large"):
        parse_frame(frame_bytes(), max_bytes=8, max_header_bytes=MAX_HEADER)


def test_rejects_frame_header_over_limit():
    with pytest.raises(ProtocolError, match="header"):
        parse_frame(frame_bytes(), MAX_FRAME, max_header_bytes=4)


def test_rejects_frame_header_running_past_end():
    raw = struct.pack("<I", 500) + b"{}"
    with pytest.raises(ProtocolError, match="header"):
        parse_frame(raw, MAX_FRAME, MAX_HEADER)


def test_rejects_frame_with_bad_header_json():
    body = b"not json"
    raw = struct.pack("<I", len(body)) + body + TINY_JPEG
    with pytest.raises(ProtocolError, match="header"):
        parse_frame(raw, MAX_FRAME, MAX_HEADER)


def test_rejects_frame_with_invalid_header_fields():
    body = json.dumps({"seq": -1, "t": 1.0, "w": 640, "h": 480}).encode()
    raw = struct.pack("<I", len(body)) + body + TINY_JPEG
    with pytest.raises(ProtocolError, match="header"):
        parse_frame(raw, MAX_FRAME, MAX_HEADER)


def test_rejects_frame_that_is_not_jpeg():
    raw = encode_frame(FrameHeader(seq=0, t=1.0, w=640, h=480), b"GIF89a....")
    with pytest.raises(ProtocolError, match="JPEG"):
        parse_frame(raw, MAX_FRAME, MAX_HEADER)


def test_pong_serialises_to_wire_json():
    wire = json.loads(Pong(t=5.0, server_t=9.0).to_wire())
    assert wire == {"v": 1, "type": "pong", "t": 5.0, "server_t": 9.0}
