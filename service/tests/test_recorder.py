import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from drive_service import recording_session
from drive_service.protocol import Gps, Imu
from drive_service.recorder import Recorder
from helpers import TINY_JPEG, gps_dict, imu_dict, make_frame

FIXED_NOW = datetime(2026, 11, 14, 3, 0, 0, tzinfo=UTC)
SESSION = "20261114T030000Z"
WAIT_S = 2.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _recorder(root: Path, queue_size: int = 64) -> Recorder:
    return Recorder(root, now=lambda: FIXED_NOW, queue_size=queue_size)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _meta(root: Path, session: str = SESSION) -> dict:
    return json.loads((root / session / "meta.json").read_text())


@dataclass(frozen=True)
class Gate:
    entered: threading.Event
    opened: threading.Event

    def set(self) -> None:
        self.opened.set()


@pytest.fixture
def gate(monkeypatch) -> Gate:
    """Hold the writer on each frame until the test opens the gate."""
    held = Gate(entered=threading.Event(), opened=threading.Event())
    real_write = recording_session.write_frame_file

    def gated(session_dir, number, item):
        held.entered.set()
        held.opened.wait()
        return real_write(session_dir, number, item)

    monkeypatch.setattr(recording_session, "write_frame_file", gated)
    yield held
    held.set()  # never leave a writer thread blocked


@pytest.mark.anyio
async def test_records_frames_and_sensors_to_a_session_folder(tmp_path):
    recorder = _recorder(tmp_path)
    started = recorder.start({"camera": "Back Ultra Wide Camera"})
    assert started.active is True
    assert started.session == SESSION

    recorder.write_frame(make_frame(seq=5, t=111.0), 1000.0)
    recorder.write_frame(make_frame(seq=6, t=222.0), 1100.0)
    recorder.write_sensor(Gps.model_validate(gps_dict(lat=12.0)), 1050.0)
    recorder.write_sensor(Imu.model_validate(imu_dict()), 1060.0)
    stopped = await recorder.stop()

    assert stopped.active is False
    assert stopped.finishing is False
    assert stopped.frames == 2
    assert stopped.sensors == 2
    assert stopped.dropped == 0
    assert stopped.error is None

    session = tmp_path / SESSION
    assert (session / "frames" / "000001.jpg").read_bytes() == TINY_JPEG
    assert (session / "frames" / "000002.jpg").read_bytes() == TINY_JPEG
    assert _lines(session / "frames.jsonl") == [
        {
            "n": 1,
            "file": "frames/000001.jpg",
            "seq": 5,
            "t": 111.0,
            "w": 640,
            "h": 480,
            "recv_ms": 1000.0,
        },
        {
            "n": 2,
            "file": "frames/000002.jpg",
            "seq": 6,
            "t": 222.0,
            "w": 640,
            "h": 480,
            "recv_ms": 1100.0,
        },
    ]
    sensors = _lines(session / "sensors.jsonl")
    assert [line["type"] for line in sensors] == ["gps", "imu"]
    assert sensors[0]["lat"] == 12.0
    assert sensors[0]["recv_ms"] == 1050.0

    meta = _meta(tmp_path)
    assert meta["protocol"] == 1
    assert meta["camera"] == "Back Ultra Wide Camera"
    assert meta["started_utc"] == "2026-11-14T03:00:00+00:00"
    assert meta["ended_utc"] == "2026-11-14T03:00:00+00:00"
    assert meta["frames"] == 2
    assert meta["sensors"] == 2
    assert meta["error"] is None


@pytest.mark.anyio
async def test_full_queue_counts_drops_instead_of_blocking(tmp_path, gate):
    recorder = _recorder(tmp_path, queue_size=1)
    recorder.start({})
    recorder.write_frame(make_frame(seq=0), 1.0)
    assert gate.entered.wait(WAIT_S)  # the worker holds frame 0 at the gate
    for seq in range(1, 5):
        recorder.write_frame(make_frame(seq=seq), 1.0)
    # Frame 1 waits in the one-slot queue; frames 2 to 4 are dropped.
    assert recorder.info.dropped == 3
    gate.set()
    stopped = await recorder.stop()
    assert stopped.frames == 2
    assert stopped.dropped == 3
