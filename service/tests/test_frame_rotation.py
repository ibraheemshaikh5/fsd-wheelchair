"""Frames carry how the phone rotated them (rot), and recordings keep it per frame."""

import json
import struct
from pathlib import Path

import pytest

from drive_service.protocol import FrameError, FrameHeader, encode_frame, parse_frame
from drive_service.recording_session import frame_item, write_frame_file
from helpers import TINY_JPEG

MAX_BYTES = 1_000_000
MAX_HEADER = 1024


def _parse(header: dict):
    return parse_frame(_raw(header), MAX_BYTES, MAX_HEADER)


def _raw(header: dict) -> bytes:
    header_bytes = json.dumps(header).encode()
    return struct.pack("<I", len(header_bytes)) + header_bytes + TINY_JPEG


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_header_accepts_quarter_turns(rot):
    frame = _parse({"seq": 1, "t": 1.0, "w": 640, "h": 360, "rot": rot})
    assert frame.header.rot == rot


def test_header_without_rot_still_parses():
    frame = _parse({"seq": 1, "t": 1.0, "w": 640, "h": 360})
    assert frame.header.rot is None


@pytest.mark.parametrize("rot", [45, -90, 360, "90", True])
def test_header_refuses_other_rotations_but_keeps_the_seq_for_the_ack(rot):
    with pytest.raises(FrameError) as error:
        _parse({"seq": 7, "t": 1.0, "w": 640, "h": 360, "rot": rot})
    assert error.value.seq == 7


def test_encode_frame_leaves_out_an_unset_rot():
    raw = encode_frame(FrameHeader(seq=1, t=1.0, w=640, h=360), TINY_JPEG)
    assert b"rot" not in raw


def test_recording_keeps_rot_per_frame(tmp_path: Path):
    (tmp_path / "frames").mkdir()
    rotated = _parse({"seq": 1, "t": 1.0, "w": 640, "h": 360, "rot": 90})
    plain = _parse({"seq": 2, "t": 2.0, "w": 640, "h": 360})
    rotated_line = json.loads(write_frame_file(tmp_path, 1, frame_item(rotated, 5.0)))
    plain_line = json.loads(write_frame_file(tmp_path, 2, frame_item(plain, 6.0)))
    assert rotated_line["rot"] == 90
    assert "rot" not in plain_line
    # The fields the model's loader reads are unchanged.
    assert {"n", "file", "seq", "t", "w", "h", "recv_ms"} <= set(plain_line)
