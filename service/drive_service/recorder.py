"""Write a drive to disk: numbered JPEG frames plus JSON-lines sensor logs.

Session layout::

    <record_dir>/<UTC timestamp>/
        meta.json        protocol version, start/end time, counts, camera info,
                         error (a failure) and end_reason (a limit was reached)
        frames/000001.jpg ...
        frames.jsonl     one line per frame: file, phone seq, capture time, receive time
        sensors.jsonl    one line per sensor message, as received, plus recv_ms,
                         and service-derived lines (type "motion"), also with recv_ms

Disk writes happen on a per-session worker thread so a slow disk cannot stall
ingest. If the worker falls behind, items are dropped and counted rather than
queued without bound. Frame count, total size and free disk space are capped;
reaching a cap ends the recording cleanly with an end_reason.

The Recorder is only used from the event loop thread. Stopping never blocks the
loop: the session is detached at once and the wait for its writer runs in a
worker thread, bounded by STOP_JOIN_TIMEOUT_S. A recording that ends at a limit
gets the same bounded wait on a watcher thread, so a hung writer is reported
either way, and ``shutdown`` waits for both kinds.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drive_service.config import Settings
from drive_service.protocol import PROTOCOL_VERSION, Frame
from drive_service.recording_session import (
    FrameItem,
    RecorderLimits,
    RecordingSession,
    SensorItem,
    SensorMessage,
    derived_item,
    frame_item,
    free_bytes,
    sensor_item,
    write_meta,
)

__all__ = [
    "InsufficientDiskError",
    "Recorder",
    "RecorderBusyError",
    "RecorderError",
    "RecorderLimits",
    "RecordingInfo",
    "limits_from_settings",
]

log = logging.getLogger(__name__)

SESSION_NAME_FORMAT = "%Y%m%dT%H%M%SZ"
STOP_JOIN_TIMEOUT_S = 10.0
BYTES_PER_GB = 1e9


class RecorderError(RuntimeError):
    """The recorder could not do what was asked."""


class RecorderBusyError(RecorderError):
    """A recording is running or the previous one is still finishing."""


class InsufficientDiskError(RecorderError):
    """Not enough free disk space to start a recording."""


@dataclass(frozen=True, slots=True)
class RecordingInfo:
    active: bool = False
    session: str | None = None
    frames: int = 0
    sensors: int = 0
    dropped: int = 0
    error: str | None = None
    finishing: bool = False
    end_reason: str | None = None


def limits_from_settings(settings: Settings) -> RecorderLimits:
    return RecorderLimits(
        queue_size=settings.recorder_queue_size,
        max_frames=settings.recorder_max_frames,
        max_bytes=settings.recorder_max_bytes,
        max_queue_bytes=settings.recorder_max_queue_bytes,
        min_free_bytes=settings.recorder_min_free_bytes,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _join_all(threads: tuple[threading.Thread, ...], timeout_s: float) -> None:
    for thread in threads:
        thread.join(timeout_s)


class Recorder:
    def __init__(
        self, root: Path, limits: RecorderLimits, now: Callable[[], datetime] = _utc_now
    ) -> None:
        self._root = root
        self._limits = limits
        self._now = now
        # Taking data, or ended by its own writer (failure, disk) and not yet replaced.
        self._session: RecordingSession | None = None
        # Detached by stop() or a limit; its writer may still be draining.
        self._stopping: RecordingSession | None = None
        # The most recent session, reported once nothing newer exists.
        self._last: RecordingSession | None = None
        # Threads waiting (bounded) for the writers of limit-ended sessions.
        self._watchers: tuple[threading.Thread, ...] = ()

    @property
    def info(self) -> RecordingInfo:
        session = self._session or self._stopping or self._last
        return self._describe(session) if session else RecordingInfo()

    def start(self, meta: dict[str, Any]) -> RecordingInfo:
        self._check_not_busy()
        self._ensure_root()
        self._check_free_space()
        session_dir = self._create_session_dir()
        full_meta = {"protocol": PROTOCOL_VERSION, "started_utc": self._now().isoformat(), **meta}
        try:
            write_meta(session_dir, full_meta)
        except OSError as error:
            raise RecorderError(f"could not create recording metadata: {error}") from error
        session = RecordingSession(session_dir, full_meta, self._limits, self._now)
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

    async def shutdown(self) -> None:
        """Stop any recording and wait (bounded) for every writer still finishing."""
        await self.stop()
        watchers = self._watchers
        self._watchers = ()
        await asyncio.to_thread(_join_all, watchers, STOP_JOIN_TIMEOUT_S)

    def write_frame(self, frame: Frame, received_wall_ms: float) -> None:
        session = self._accepting_session()
        if session is not None:
            self._offer(session, frame_item(frame, received_wall_ms))

    def write_sensor(self, message: SensorMessage, received_wall_ms: float) -> None:
        session = self._accepting_session()
        if session is not None:
            self._offer(session, sensor_item(message, received_wall_ms))

    def write_derived(self, record: Mapping[str, Any], received_wall_ms: float) -> None:
        """Record a line the service computed, such as the motion estimate."""
        session = self._accepting_session()
        if session is not None:
            self._offer(session, derived_item(record, received_wall_ms))

    def _accepting_session(self) -> RecordingSession | None:
        session = self._session
        return None if session is None or session.closing else session

    def _offer(self, session: RecordingSession, item: FrameItem | SensorItem) -> None:
        end_reason = session.offer(item)
        if end_reason is not None:
            self._end_at_limit(session, end_reason)

    def _end_at_limit(self, session: RecordingSession, reason: str) -> None:
        """A normal limit: detach the session; its writer drains and finalises it."""
        log.info("recording ending: %s (%s)", session.session_dir, reason)
        self._session, self._stopping = None, session
        session.request_stop(reason)
        self._watch_finish(session)

    def _watch_finish(self, session: RecordingSession) -> None:
        """Wait for a detached writer off the loop, as stop() does, so a hung one is reported."""
        watcher = threading.Thread(
            target=session.finish,
            args=(STOP_JOIN_TIMEOUT_S,),
            name="recorder-finish",
            daemon=True,
        )
        self._watchers = (*(w for w in self._watchers if w.is_alive()), watcher)
        watcher.start()

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
            end_reason=session.end_reason,
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

    def _check_free_space(self) -> None:
        try:
            free = free_bytes(self._root)
        except OSError as error:
            raise RecorderError(f"could not check free disk space: {error}") from error
        if free < self._limits.min_free_bytes:
            raise InsufficientDiskError(
                f"not enough free disk space to record ({free / BYTES_PER_GB:.1f} GB free, "
                f"need {self._limits.min_free_bytes / BYTES_PER_GB:.1f} GB)"
            )

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
