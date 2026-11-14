import asyncio
import json
import shutil
import threading
import time
from collections import namedtuple
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from drive_service import recorder as recorder_module
from drive_service import recording_session
from drive_service.config import Settings
from drive_service.protocol import Gps, Imu
from drive_service.recorder import (
    InsufficientDiskError,
    Recorder,
    RecorderBusyError,
    RecorderError,
    RecorderLimits,
    limits_from_settings,
)
from helpers import TINY_JPEG, gps_dict, imu_dict, make_frame

FIXED_NOW = datetime(2026, 11, 14, 3, 0, 0, tzinfo=UTC)
SESSION = "20261114T030000Z"
WAIT_S = 2.0
DiskUsage = namedtuple("DiskUsage", "total used free")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _recorder(root: Path, **limits) -> Recorder:
    defaults = {
        "queue_size": 64,
        "max_frames": 1000,
        "max_bytes": 1_000_000_000,
        "max_queue_bytes": 1_000_000_000,
        "min_free_bytes": 0,
    }
    return Recorder(root, RecorderLimits(**{**defaults, **limits}), now=lambda: FIXED_NOW)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _meta(root: Path, session: str = SESSION) -> dict:
    return json.loads((root / session / "meta.json").read_text())


def _wait_until(condition, timeout_s: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


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


def test_idle_recorder_reports_inactive_and_ignores_writes(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.write_frame(make_frame(), 1.0)
    recorder.write_sensor(Gps.model_validate(gps_dict()), 1.0)
    info = recorder.info
    assert info.active is False
    assert info.finishing is False
    assert info.end_reason is None
    assert list(tmp_path.iterdir()) == []


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
    assert stopped.end_reason is None

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
    assert meta["end_reason"] is None


@pytest.mark.anyio
async def test_cannot_start_twice(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.start({})
    with pytest.raises(RecorderBusyError, match="already"):
        recorder.start({})
    await recorder.stop()


@pytest.mark.anyio
async def test_stop_when_idle_is_harmless(tmp_path):
    assert (await _recorder(tmp_path).stop()).active is False


@pytest.mark.anyio
async def test_second_session_in_the_same_second_gets_its_own_folder(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.start({})
    await recorder.stop()
    second = recorder.start({})
    await recorder.stop()
    assert second.session == f"{SESSION}-2"
    assert (tmp_path / f"{SESSION}-2" / "meta.json").exists()


def test_unwritable_root_fails_to_start(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    recorder = _recorder(blocker)
    with pytest.raises(RecorderError, match="could not create"):
        recorder.start({})
    assert recorder.info.active is False


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


@pytest.mark.anyio
async def test_write_failure_is_reported_not_raised(tmp_path, monkeypatch):
    recorder = _recorder(tmp_path)
    recorder.start({})

    def fail(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", fail)
    recorder.write_frame(make_frame(), 1.0)
    stopped = await recorder.stop()
    assert "disk full" in stopped.error
    assert stopped.frames == 0


# H3: stopping must not block the event loop, and must not mix up sessions.


@pytest.mark.anyio
async def test_stop_does_not_block_event_loop(tmp_path, gate):
    recorder = _recorder(tmp_path)
    recorder.start({})
    for seq in range(5):
        recorder.write_frame(make_frame(seq=seq), 1.0)
    task = asyncio.create_task(recorder.stop())
    started = time.monotonic()
    await asyncio.sleep(0.01)
    assert time.monotonic() - started < 0.1
    assert recorder.info.finishing is True
    assert recorder.info.active is False
    gate.set()
    stopped = await task
    assert stopped.frames == 5
    assert stopped.finishing is False


@pytest.mark.anyio
async def test_start_while_previous_finishing_is_refused(tmp_path, gate):
    recorder = _recorder(tmp_path)
    recorder.start({})
    for seq in range(5):
        recorder.write_frame(make_frame(seq=seq), 1.0)
    task = asyncio.create_task(recorder.stop())
    await asyncio.sleep(0.05)
    with pytest.raises(RecorderBusyError, match="still finishing"):
        recorder.start({})
    gate.set()
    await task
    meta = _meta(tmp_path)
    assert "ended_utc" in meta
    assert meta["frames"] == 5


@pytest.mark.anyio
async def test_stop_returns_when_worker_hung_and_queue_full(tmp_path, gate, monkeypatch):
    monkeypatch.setattr(recorder_module, "STOP_JOIN_TIMEOUT_S", 0.2)
    recorder = _recorder(tmp_path, queue_size=2)
    recorder.start({})
    for seq in range(6):
        recorder.write_frame(make_frame(seq=seq), 1.0)
    stopped = await asyncio.wait_for(recorder.stop(), 2 * 0.2 + 1.0)
    assert stopped.active is False
    assert stopped.finishing is True
    assert "did not finish" in stopped.error
    assert "did not finish" in _meta(tmp_path)["error"]


@pytest.mark.anyio
async def test_late_worker_cannot_touch_new_session(tmp_path, gate, monkeypatch):
    monkeypatch.setattr(recorder_module, "STOP_JOIN_TIMEOUT_S", 0.2)
    recorder = _recorder(tmp_path)
    recorder.start({})
    for seq in range(5):
        recorder.write_frame(make_frame(seq=seq), 1.0)
    await recorder.stop()  # times out with the writer still held
    with pytest.raises(RecorderBusyError, match="still finishing"):
        recorder.start({})

    gate.set()
    _wait_until(lambda: recorder.info.finishing is False)
    meta = _meta(tmp_path)
    assert meta["frames"] == 5
    assert meta["error"] is None

    fresh = recorder.start({})
    assert fresh.session == f"{SESSION}-2"
    assert (fresh.frames, fresh.error) == (0, None)
    await recorder.stop()


# M5: any writer crash is reported, not only OSError.


@pytest.mark.anyio
async def test_unexpected_writer_exception_is_reported(tmp_path, monkeypatch):
    def boom(session_dir, number, item):
        raise ValueError("boom")

    monkeypatch.setattr(recording_session, "write_frame_file", boom)
    recorder = _recorder(tmp_path)
    recorder.start({})
    recorder.write_frame(make_frame(), 1.0)
    _wait_until(lambda: recorder.info.error is not None)
    assert "boom" in recorder.info.error
    assert recorder.info.active is False
    # Nothing more is queued into a dead writer.
    recorder.write_frame(make_frame(), 1.0)
    stopped = await recorder.stop()
    assert "boom" in stopped.error
    assert "boom" in _meta(tmp_path)["error"]


# L2 / M6: limits end the recording cleanly instead of reporting an error.


def test_frame_limit_ends_recording_cleanly(tmp_path):
    recorder = _recorder(tmp_path, max_frames=2)
    recorder.start({})
    for seq in range(3):
        recorder.write_frame(make_frame(seq=seq), 1.0)
    recorder.write_sensor(Gps.model_validate(gps_dict()), 1.0)
    _wait_until(lambda: recorder.info.finishing is False)
    info = recorder.info
    assert info.active is False
    assert info.error is None
    assert "frame limit" in info.end_reason
    meta = _meta(tmp_path)
    assert meta["frames"] == 2
    assert meta["sensors"] == 0
    assert "frame limit" in meta["end_reason"]
    assert meta["error"] is None


@pytest.mark.anyio
async def test_new_recording_can_start_after_a_limit_ended_one(tmp_path):
    recorder = _recorder(tmp_path, max_frames=1)
    recorder.start({})
    recorder.write_frame(make_frame(), 1.0)
    recorder.write_frame(make_frame(), 1.0)
    _wait_until(lambda: recorder.info.finishing is False)
    second = recorder.start({})
    assert second.active is True
    assert second.end_reason is None
    await recorder.stop()


def test_byte_budget_ends_recording(tmp_path):
    recorder = _recorder(tmp_path, max_bytes=5000)
    recorder.start({})
    for seq in range(10):
        recorder.write_frame(make_frame(seq=seq, jpeg=TINY_JPEG + b"\x00" * 1000), 1.0)
    _wait_until(lambda: recorder.info.finishing is False)
    info = recorder.info
    assert info.active is False
    assert info.error is None
    assert "size" in info.end_reason
    assert len(list((tmp_path / SESSION / "frames").iterdir())) <= 5


def test_sensor_flood_is_capped(tmp_path):
    recorder = _recorder(tmp_path, max_bytes=10_000)
    recorder.start({})
    gps = Gps.model_validate(gps_dict())
    for _ in range(1000):
        recorder.write_sensor(gps, 1.0)
    _wait_until(lambda: recorder.info.finishing is False)
    assert (tmp_path / SESSION / "sensors.jsonl").stat().st_size <= 10_000
    assert "size" in recorder.info.end_reason


@pytest.mark.anyio
async def test_queue_byte_cap_drops_instead_of_queueing(tmp_path, gate):
    frame = make_frame(jpeg=TINY_JPEG + b"\x00" * 1000)
    recorder = _recorder(tmp_path, max_queue_bytes=2500)
    recorder.start({})
    for _ in range(5):
        recorder.write_frame(frame, 1.0)
    assert recorder.info.dropped == 3
    gate.set()
    stopped = await recorder.stop()
    assert (stopped.frames, stopped.dropped) == (2, 3)


def test_low_free_space_refuses_start(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda path: DiskUsage(100, 100, 0))
    recorder = _recorder(tmp_path, min_free_bytes=1000)
    with pytest.raises(InsufficientDiskError, match="free disk"):
        recorder.start({})
    assert recorder.info.active is False
    assert not any(tmp_path.iterdir())


def test_disk_filling_up_ends_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(recording_session, "DISK_CHECK_EVERY_ITEMS", 1)
    recorder = _recorder(tmp_path, min_free_bytes=1000)
    recorder.start({})
    monkeypatch.setattr(shutil, "disk_usage", lambda path: DiskUsage(100, 100, 0))
    recorder.write_frame(make_frame(), 1.0)
    _wait_until(lambda: recorder.info.finishing is False and not recorder.info.active)
    info = recorder.info
    assert info.error is None
    assert "disk" in info.end_reason
    assert "disk" in _meta(tmp_path)["end_reason"]


def test_limits_come_from_settings(tmp_path):
    settings = Settings(record_dir=tmp_path)
    limits = limits_from_settings(settings)
    assert limits.max_bytes == 20_000_000_000
    assert limits.max_queue_bytes == 64_000_000
    assert limits.min_free_bytes == 2_000_000_000
    assert limits.max_frames == settings.recorder_max_frames
    assert limits.queue_size == settings.recorder_queue_size
