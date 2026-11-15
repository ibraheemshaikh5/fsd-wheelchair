"""Write a drive to disk: numbered JPEG frames plus JSON-lines sensor logs.

Session layout::

    <record_dir>/<UTC timestamp>/
        meta.json        protocol version, start/end time, counts, camera info,
                         and error (a failure)
        frames/000001.jpg ...
        frames.jsonl     one line per frame: file, phone seq, capture time, receive time
        sensors.jsonl    one line per sensor message, as received, plus recv_ms,
                         and service-derived lines (type "motion"), also with recv_ms

Disk writes happen on a per-session worker thread so a slow disk cannot stall
ingest. If the worker falls behind, items are dropped and counted rather than
queued without bound.

The Recorder is only used from the event loop thread. Stopping never blocks the
loop: the session is detached at once and the wait for its writer runs in a
worker thread, bounded by STOP_JOIN_TIMEOUT_S.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drive_service.config import RECORDER_QUEUE_SIZE
from drive_service.protocol import PROTOCOL_VERSION, Frame
from drive_service.recording_session import (
    FrameItem,
    RecordingSession,
    SensorMessage,
    derived_item,
    sensor_item,
    write_meta,
)

log = logging.getLogger(__name__)

SESSION_NAME_FORMAT = "%Y%m%dT%H%M%SZ"
STOP_JOIN_TIMEOUT_S = 10.0


class RecorderError(RuntimeError):
    """The recorder could not do what was asked."""


class RecorderBusyError(RecorderError):
    """A recording is running or the previous one is still finishing."""


@dataclass(frozen=True, slots=True)
class RecordingInfo:
    active: bool = False
    session: str | None = None
    frames: int = 0
    sensors: int = 0
    dropped: int = 0
    error: str | None = None
    finishing: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Recorder:
    def __init__(
        self,
        root: Path,
        now: Callable[[], datetime] = _utc_now,
        queue_size: int = RECORDER_QUEUE_SIZE,
    ) -> None:
        self._root = root
        self._now = now
        self._queue_size = queue_size
        # Taking data, or ended by its own writer (a failure) and not yet replaced.
        self._session: RecordingSession | None = None
        # Detached by stop(); its writer may still be draining.
        self._stopping: RecordingSession | None = None
        # The most recent session, reported once nothing newer exists.
        self._last: RecordingSession | None = None

    @property
    def info(self) -> RecordingInfo:
        session = self._session or self._stopping or self._last
        return self._describe(session) if session else RecordingInfo()

    def start(self, meta: dict[str, Any]) -> RecordingInfo:
        self._check_not_busy()
        self._ensure_root()
        session_dir = self._create_session_dir()
        full_meta = {"protocol": PROTOCOL_VERSION, "started_utc": self._now().isoformat(), **meta}
        try:
            write_meta(session_dir, full_meta)
        except OSError as error:
            raise RecorderError(f"could not create recording metadata: {error}") from error
        session = RecordingSession(session_dir, full_meta, self._now, self._queue_size)
        self._session, self._stopping, self._last = session, None, session
        session.start()
        log.info("recording started: %s", session_dir)
        return self._describe(session)

    async def stop(self) -> RecordingInfo:
        """End the current recording and wait (off the event loop) for its writer."""
        session = self._session
        if session is None:
            return self.info
        # No await between these lines: the session is detached before anything else runs.
        self._session, self._stopping = None, session
        session.request_stop()
        try:
            await asyncio.to_thread(session.finish, STOP_JOIN_TIMEOUT_S)
        finally:
            # A writer that timed out stays in _stopping, so start() is refused until it exits.
            if self._stopping is session and session.done:
                self._stopping = None
        log.info("recording stopped: %s", session.session_dir)
        return self._describe(session)

    def write_frame(self, frame: Frame, received_wall_ms: float) -> None:
        session = self._accepting_session()
        if session is not None:
            session.offer(FrameItem(frame, received_wall_ms))

    def write_sensor(self, message: SensorMessage, received_wall_ms: float) -> None:
        session = self._accepting_session()
        if session is not None:
            session.offer(sensor_item(message, received_wall_ms))

    def write_derived(self, record: Mapping[str, Any], received_wall_ms: float) -> None:
        """Record a line the service computed, such as the motion estimate."""
        session = self._accepting_session()
        if session is not None:
            session.offer(derived_item(record, received_wall_ms))

    def _accepting_session(self) -> RecordingSession | None:
        session = self._session
        return None if session is None or session.closing else session

    def _describe(self, session: RecordingSession) -> RecordingInfo:
        frames, sensors = session.written
        return RecordingInfo(
            active=session is self._session and not session.closing,
            session=session.name,
            frames=frames,
            sensors=sensors,
            dropped=session.dropped,
            error=session.error,
            finishing=session.closing and not session.done,
        )

    def _check_not_busy(self) -> None:
        if self._session is not None and not self._session.closing:
            raise RecorderBusyError("a recording is already running")
        for session in (self._session, self._stopping):
            if session is not None and not session.done:
                raise RecorderBusyError("previous recording is still finishing")

    def _ensure_root(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RecorderError(f"could not create recording folder: {error}") from error

    def _create_session_dir(self) -> Path:
        base = self._now().strftime(SESSION_NAME_FORMAT)
        suffix = 1
        while True:
            name = base if suffix == 1 else f"{base}-{suffix}"
            session_dir = self._root / name
            try:
                (session_dir / "frames").mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                suffix += 1
                continue
            except OSError as error:
                raise RecorderError(f"could not create recording folder: {error}") from error
            return session_dir
