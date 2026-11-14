"""A small recording on disk, in the layout the driving service writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

START_MS = 1_794_650_000_000.0
FRAME_GAP_MS = 50.0
FRAME_COUNT = 6


def write_recording(folder: Path) -> Path:
    (folder / "frames").mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"protocol": 1, "frames": FRAME_COUNT}))

    frame_lines = []
    for index in range(FRAME_COUNT):
        number = index + 1
        relative = f"frames/{number:06d}.jpg"
        Image.new("RGB", (64, 36), (index * 40, 0, 0)).save(folder / relative)
        frame_lines.append(
            {
                "n": number,
                "file": relative,
                "seq": 100 + index,
                "t": START_MS + index * FRAME_GAP_MS,
                "w": 64,
                "h": 36,
                "recv_ms": START_MS + index * FRAME_GAP_MS + 40,
            }
        )
    (folder / "frames.jsonl").write_text("".join(json.dumps(line) + "\n" for line in frame_lines))

    sensors = [
        {
            "type": "gps",
            "t": START_MS + 60,
            "fix_t": START_MS + 10,
            "lat": 38.99,
            "lon": -76.94,
            "accuracy_m": 4.0,
            "speed_mps": 1.1,
            "course_deg": 90.0,
        },
        {"type": "heading", "t": START_MS + 120, "magnetic_deg": 95.0, "accuracy_deg": 10.0},
        {
            "type": "imu",
            "t": START_MS + 100,
            "samples": [[0, 0, 0, 9.8, 0, 0, 1.0], [10, 0, 0, 9.8, 0, 0, 2.0]],
        },
        # Unknown message kinds must be ignored, not fatal.
        {"type": "hello", "t": START_MS},
    ]
    (folder / "sensors.jsonl").write_text("".join(json.dumps(line) + "\n" for line in sensors))
    return folder


@pytest.fixture
def recording_dir(tmp_path: Path) -> Path:
    return write_recording(tmp_path / "20261114T150000Z")
