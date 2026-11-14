"""Every item offered to a recording is either written or counted as dropped."""

import json
import queue
import shutil
import threading
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

from drive_service import recording_session
from drive_service.recording_session import RecorderLimits, RecordingSession, frame_item
from helpers import make_frame

FIXED_NOW = datetime(2026, 11, 14, 3, 0, 0, tzinfo=UTC)
JOIN_S = 5.0
DiskUsage = namedtuple("DiskUsage", "total used free")
LIMITS = RecorderLimits(
    queue_size=8,
    max_frames=100,
    max_bytes=1_000_000_000,
    max_queue_bytes=100_000_000,
    min_free_bytes=1000,
)


def _session(tmp_path: Path) -> RecordingSession:
    (tmp_path / "frames").mkdir()
    return RecordingSession(tmp_path, {}, LIMITS, lambda: FIXED_NOW)


def _meta(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "meta.json").read_text())


def _accounted(session: RecordingSession) -> int:
    return session.written[0] + session.dropped


def test_item_offered_between_empty_poll_and_closing_check_is_written(tmp_path):
    """The loop offers and stops in the gap after the writer's poll came back empty."""
    session = _session(tmp_path)
    fired = threading.Event()

    class RacingQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            try:
                return super().get(block, timeout)
            except queue.Empty:
                if not fired.is_set():
                    fired.set()
                    session.offer(frame_item(make_frame(), 1.0))
                    session.request_stop()
                raise

    session._queue = RacingQueue(maxsize=LIMITS.queue_size)
    session.start()
    session.finish(JOIN_S)
    assert session.frames_offered == 1
    assert session.written == (1, 0)
    assert session.dropped == 0
    assert _meta(tmp_path)["frames"] == 1


def test_items_left_when_the_disk_fills_are_counted_as_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(recording_session, "DISK_CHECK_EVERY_ITEMS", 1)
    entered, opened = threading.Event(), threading.Event()
    real_write = recording_session.write_frame_file

    def gated(session_dir, number, item):
        entered.set()
        opened.wait(JOIN_S)
        return real_write(session_dir, number, item)

    monkeypatch.setattr(recording_session, "write_frame_file", gated)
    session = _session(tmp_path)
    session.start()
    session.offer(frame_item(make_frame(seq=0), 1.0))
    assert entered.wait(JOIN_S)
    for seq in range(1, 4):
        session.offer(frame_item(make_frame(seq=seq), 1.0))
    monkeypatch.setattr(shutil, "disk_usage", lambda path: DiskUsage(100, 100, 0))
    opened.set()
    session.finish(JOIN_S)
    assert session.end_reason == recording_session.DISK_LOW_REASON
    assert session.written == (1, 0)
    assert session.dropped == 3
    assert _accounted(session) == session.frames_offered
    meta = _meta(tmp_path)
    assert (meta["frames"], meta["dropped"]) == (1, 3)


def test_offer_after_the_writer_exited_is_counted_as_dropped(tmp_path):
    session = _session(tmp_path)
    session.start()
    session.request_stop()
    session.finish(JOIN_S)
    assert session.done
    session.offer(frame_item(make_frame(), 1.0))
    assert session.dropped == 1
    assert session._queue.qsize() == 0
