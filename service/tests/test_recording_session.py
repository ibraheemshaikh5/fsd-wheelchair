"""Every item offered to a recording is either written or counted as dropped."""

import json
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path

from drive_service import recording_session
from drive_service.recording_session import FrameItem, RecordingSession
from helpers import make_frame

FIXED_NOW = datetime(2026, 11, 14, 3, 0, 0, tzinfo=UTC)
JOIN_S = 5.0
QUEUE_SIZE = 8


def _session(tmp_path: Path) -> RecordingSession:
    (tmp_path / "frames").mkdir()
    return RecordingSession(tmp_path, {}, lambda: FIXED_NOW, QUEUE_SIZE)


def _meta(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "meta.json").read_text())


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
                    session.offer(FrameItem(make_frame(), 1.0))
                    session.request_stop()
                raise

    session._queue = RacingQueue(maxsize=QUEUE_SIZE)
    session.start()
    session.finish(JOIN_S)
    assert session.written == (1, 0)
    assert session.dropped == 0
    assert _meta(tmp_path)["frames"] == 1


def test_items_left_when_the_writer_fails_are_counted_as_dropped(tmp_path, monkeypatch):
    entered, opened = threading.Event(), threading.Event()

    def failing(session_dir, number, item):
        entered.set()
        opened.wait(JOIN_S)
        raise OSError("disk full")

    monkeypatch.setattr(recording_session, "write_frame_file", failing)
    session = _session(tmp_path)
    session.start()
    session.offer(FrameItem(make_frame(seq=0), 1.0))
    assert entered.wait(JOIN_S)
    for seq in range(1, 4):
        session.offer(FrameItem(make_frame(seq=seq), 1.0))
    opened.set()
    session.finish(JOIN_S)
    assert "disk full" in session.error
    assert session.written == (0, 0)
    # The frame being written when the disk failed is lost, not dropped from the queue.
    assert session.dropped == 3
    meta = _meta(tmp_path)
    assert (meta["frames"], meta["dropped"]) == (0, 3)


def test_offer_after_the_writer_exited_is_counted_as_dropped(tmp_path):
    session = _session(tmp_path)
    session.start()
    session.request_stop()
    session.finish(JOIN_S)
    assert session.done
    session.offer(FrameItem(make_frame(), 1.0))
    assert session.dropped == 1
    assert session._queue.qsize() == 0
