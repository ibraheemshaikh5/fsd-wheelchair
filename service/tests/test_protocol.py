import json
import struct

import pytest

from drive_service.protocol import (
    FrameHeader,
    ProtocolError,
    encode_frame,
    parse_frame,
)
from helpers import (
    TINY_JPEG,
    frame_bytes,
)

MAX_FRAME = 1_000_000
MAX_HEADER = 1024


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
