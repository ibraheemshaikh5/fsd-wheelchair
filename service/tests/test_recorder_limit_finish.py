"""A recording that ends at a limit is waited for like a stopped one."""

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from drive_service import recorder as recorder_module
from drive_service import recording_session
from drive_service.recorder import Recorder, RecorderLimits
from drive_service.recording_session import WRITER_TIMEOUT_ERROR
from helpers import make_frame

FIXED_NOW = datetime(2026, 11, 14, 3, 0, 0, tzinfo=UTC)
SESSION = "20261114T030000Z"
WAIT_S = 3.0
SHORT_JOIN_S = 0.2
OPEN_GATE_AFTER_S = 0.2


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def gate(monkeypatch):
    """Hold the writer on each frame until the test opens the gate."""
    opened = threading.Event()
    real_write = recording_session.write_frame_file

    def gated(session_dir, number, item):
        opened.wait()
        return real_write(session_dir, number, item)

    monkeypatch.setattr(recording_session, "write_frame_file", gated)
    yield opened
    opened.set()  # never leave a writer thread blocked


def _recorder(root: Path) -> Recorder:
    limits = RecorderLimits(
        queue_size=64,
        max_frames=1,
        max_bytes=1_000_000_000,
        max_queue_bytes=1_000_000_000,
        min_free_bytes=0,
    )
    return Recorder(root, limits, now=lambda: FIXED_NOW)


def _meta(root: Path) -> dict:
    return json.loads((root / SESSION / "meta.json").read_text())


def _wait_until(condition) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def _end_at_frame_limit(recorder: Recorder) -> None:
    recorder.start({})
    recorder.write_frame(make_frame(seq=0), 1.0)
    recorder.write_frame(make_frame(seq=1), 1.0)  # over max_frames: ends the recording


@pytest.mark.anyio
async def test_hung_writer_after_a_limit_reports_the_timeout(tmp_path, gate, monkeypatch):
    monkeypatch.setattr(recorder_module, "STOP_JOIN_TIMEOUT_S", SHORT_JOIN_S)
    recorder = _recorder(tmp_path)
    _end_at_frame_limit(recorder)
    _wait_until(lambda: recorder.info.error == WRITER_TIMEOUT_ERROR)
    info = recorder.info
    assert info.finishing is True
    assert "frame limit" in info.end_reason
    meta = _meta(tmp_path)
    assert meta["error"] == WRITER_TIMEOUT_ERROR
    assert "ended_utc" in meta
    gate.set()
    _wait_until(lambda: recorder.info.finishing is False)
    assert recorder.info.error is None
    assert _meta(tmp_path)["frames"] == 1


@pytest.mark.anyio
async def test_shutdown_waits_for_a_limit_ended_writer(tmp_path, gate):
    recorder = _recorder(tmp_path)
    _end_at_frame_limit(recorder)
    threading.Timer(OPEN_GATE_AFTER_S, gate.set).start()
    await recorder.shutdown()
    assert recorder.info.finishing is False
    meta = _meta(tmp_path)
    assert meta["frames"] == 1
    assert "frame limit" in meta["end_reason"]


@pytest.mark.anyio
async def test_shutdown_stops_a_running_recording(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.start({})
    await recorder.shutdown()
    assert recorder.info.active is False
    assert recorder.info.finishing is False
    assert "ended_utc" in _meta(tmp_path)
