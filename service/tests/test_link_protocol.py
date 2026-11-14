"""The link message (phone -> service), the ack reply, and the seq a refused frame carries."""

import json
import struct

import pytest

from drive_service.protocol import (
    MAX_LINK_COUNT,
    MAX_LINK_IN_FLIGHT,
    Ack,
    FrameError,
    FrameHeader,
    Link,
    ProtocolError,
    encode_frame,
    parse_frame,
    parse_text,
)
from helpers import TINY_JPEG, as_json, frame_bytes, link_dict

MAX_TEXT = 64_000
MAX_FRAME = 1_000_000
MAX_HEADER = 1024


def test_link_message_parses():
    link = parse_text(as_json(link_dict()), MAX_TEXT)
    assert isinstance(link, Link)
    assert link.captured == 600
    assert link.skipped_flow == 300
    assert link.jpeg_quality == 0.5
    assert link.ack_rtt_ms == 180.0


def test_link_round_trip_may_be_unknown():
    assert parse_text(as_json(link_dict(ack_rtt_ms=None)), MAX_TEXT).ack_rtt_ms is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"captured": -1},
        {"sent": -1},
        {"acked": -1},
        {"skipped_flow": -1},
        {"skipped_encoder": -1},
        {"skipped_socket": -1},
        {"in_flight": -1},
        {"captured": MAX_LINK_COUNT + 1},
        {"in_flight": MAX_LINK_IN_FLIGHT + 1},
        {"sent": 1.5},
        {"captured": True},
        {"sent": False},
        {"acked": True},
        {"skipped_flow": True},
        {"skipped_encoder": True},
        {"skipped_socket": True},
        {"in_flight": True},
        {"captured": "5"},
        {"jpeg_quality": 0.0},
        {"jpeg_quality": 1.01},
        {"ack_rtt_ms": -1.0},
        {"t": 0},
        {"v": 2},
        {"bitrate": 5},
    ],
)
def test_link_rejects_out_of_bounds_or_unknown_fields(overrides):
    with pytest.raises(ProtocolError):
        parse_text(as_json(link_dict(**overrides)), MAX_TEXT)


def test_link_requires_every_counter():
    message = {key: value for key, value in link_dict().items() if key != "acked"}
    with pytest.raises(ProtocolError, match="acked"):
        parse_text(as_json(message), MAX_TEXT)


def test_ack_serialises_to_wire_json():
    wire = json.loads(Ack(seq=17, t=1_700_000_000_123.5).to_wire())
    assert wire == {"v": 1, "type": "ack", "seq": 17, "t": 1_700_000_000_123.5}


def test_frame_that_is_not_jpeg_carries_its_seq():
    raw = encode_frame(FrameHeader(seq=41, t=1.0, w=640, h=480), b"GIF89a....")
    with pytest.raises(FrameError, match="JPEG") as refused:
        parse_frame(raw, MAX_FRAME, MAX_HEADER)
    assert refused.value.seq == 41


def test_oversized_frame_with_a_readable_header_carries_its_seq():
    with pytest.raises(FrameError, match="too large") as refused:
        parse_frame(frame_bytes(seq=9), max_bytes=40, max_header_bytes=MAX_HEADER)
    assert refused.value.seq == 9


def test_oversized_frame_with_an_unreadable_header_has_no_seq():
    raw = struct.pack("<I", 4) + b"nope" + TINY_JPEG
    with pytest.raises(FrameError, match="too large") as refused:
        parse_frame(raw, max_bytes=10, max_header_bytes=MAX_HEADER)
    assert refused.value.seq is None


def _raw_frame(header: dict) -> bytes:
    header_bytes = json.dumps(header).encode()
    return struct.pack("<I", len(header_bytes)) + header_bytes + TINY_JPEG


@pytest.mark.parametrize(
    "header",
    [
        {"seq": 7, "t": 1.0, "w": 0, "h": 10},
        {"seq": 7, "t": 1.0, "w": 640, "h": 9000},
        {"seq": 7, "t": 0, "w": 640, "h": 480},
        {"seq": 7, "t": 1.0, "w": 640, "h": 480, "exposure_ms": 4},
        {"seq": 7},
    ],
)
def test_invalid_header_with_a_readable_seq_carries_it(header):
    with pytest.raises(FrameError, match="header invalid") as refused:
        parse_frame(_raw_frame(header), MAX_FRAME, MAX_HEADER)
    assert refused.value.seq == 7


@pytest.mark.parametrize(
    "header",
    [
        {"seq": -1, "t": 1.0, "w": 640, "h": 480},
        {"seq": True, "t": 1.0, "w": 0, "h": 480},
        {"seq": "7", "t": 1.0, "w": 0, "h": 480},
        {"seq": 7.5, "t": 1.0, "w": 640, "h": 480},
        {"seq": None, "t": 1.0, "w": 640, "h": 480},
        {"t": 1.0, "w": 640, "h": 480},
        [7, 1.0, 640, 480],
        7,
    ],
)
def test_invalid_header_without_a_usable_seq_has_none(header):
    with pytest.raises(FrameError, match="header invalid") as refused:
        parse_frame(_raw_frame(header), MAX_FRAME, MAX_HEADER)
    assert refused.value.seq is None


def test_deeply_nested_header_has_no_seq():
    raw = struct.pack("<I", 1000) + b"[" * 1000 + TINY_JPEG
    with pytest.raises(FrameError) as refused:
        parse_frame(raw, MAX_FRAME, 2000)
    assert refused.value.seq is None


@pytest.mark.parametrize(
    "raw",
    [
        b"\x01\x02",
        struct.pack("<I", 500) + b"{}",
        struct.pack("<I", 8) + b"not json" + TINY_JPEG,
        struct.pack("<I", 2) + b"{}" + TINY_JPEG,
    ],
)
def test_unparseable_header_has_no_seq(raw):
    with pytest.raises(FrameError) as refused:
        parse_frame(raw, MAX_FRAME, MAX_HEADER)
    assert refused.value.seq is None


def test_frame_error_is_a_protocol_error():
    assert issubclass(FrameError, ProtocolError)
