"""One recording: its folder, bounded queue, writer thread and counters.

Each session owns everything it touches, so a writer that finishes late can only
ever update its own session, never the one started after it.

Threading rules:

* The event loop calls ``offer`` and ``request_stop``. It alone writes ``_refused``.
* The writer thread drains the queue. It alone writes ``written``, its own
  error, ``_abandoned`` and the final ``meta.json``.
* Every offered item is either written or counted in ``dropped``. ``offer``
  puts under ``_seal_lock``, and the writer seals the queue under the same lock
  before it exits: it only exits through an empty queue when stopping, and any
  item still queued when it exits early (a failure) is counted as abandoned. An
  offer after the seal, or while the queue is full, is refused and counted.
* ``meta.json`` is finalised under ``_meta_lock``: by the writer when it exits,
  or by ``finish`` if the writer did not exit in time (the writer then rewrites
  it with the real counts once it does).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from drive_service.protocol import Frame, Gps, Heading, Hello, Imu, Link

log = logging.getLogger(__name__)

SensorMessage = Hello | Gps | Heading | Imu | Link

QUEUE_POLL_S = 0.1
WRITER_TIMEOUT_ERROR = "recording writer did not finish in time"


@dataclass(frozen=True, slots=True)
class FrameItem:
    frame: Frame
    received_wall_ms: float


@dataclass(frozen=True, slots=True)
class SensorItem:
    line: str


def sensor_item(message: SensorMessage, received_wall_ms: float) -> SensorItem:
    return SensorItem(json.dumps({"recv_ms": received_wall_ms, **message.model_dump()}) + "\n")


def derived_item(record: Mapping[str, Any], received_wall_ms: float) -> SensorItem:
    """A line the service computed itself (e.g. motion), stored beside the phone's."""
    return SensorItem(json.dumps({"recv_ms": received_wall_ms, **record}) + "\n")


def write_frame_file(session_dir: Path, number: int, item: FrameItem) -> str:
    """Write one JPEG and return its line for frames.jsonl."""
    relative = f"frames/{number:06d}.jpg"
    (session_dir / relative).write_bytes(item.frame.jpeg)
    header = item.frame.header
    line = {
        "n": number,
        "file": relative,
        "seq": header.seq,
        "t": header.t,
        "w": header.w,
        "h": header.h,
        "recv_ms": item.received_wall_ms,
    }
    if header.rot is not None:
        line["rot"] = header.rot
    return json.dumps(line) + "\n"


def write_meta(session_dir: Path, meta: dict[str, Any]) -> None:
    (session_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")


class RecordingSession:
    def __init__(
        self,
        session_dir: Path,
        meta: dict[str, Any],
        now: Callable[[], datetime],
        queue_size: int,
    ) -> None:
        self.session_dir = session_dir
        self._meta = meta
        self._now = now
        self._queue: queue.Queue[FrameItem | SensorItem] = queue.Queue(maxsize=queue_size)
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._stop_requested = threading.Event()
        self._done = threading.Event()
        self._meta_lock = threading.Lock()
        self._seal_lock = threading.Lock()
        self._sealed = False
        # Loop side.
        self._refused = 0
        # Writer side. ``written`` is (frames, sensor lines), replaced whole.
        self.written: tuple[int, int] = (0, 0)
        self._worker_error: str | None = None
        self._abandoned = 0
        # Under _meta_lock.
        self._timed_out = False
        self._writer_exited = False

    @property
    def name(self) -> str:
        return self.session_dir.name

    @property
    def closing(self) -> bool:
        """No longer taking data: stopped or failed."""
        return self._stop_requested.is_set()

    @property
    def done(self) -> bool:
        """The writer has exited and written the final meta.json."""
        return self._done.is_set()

    @property
    def dropped(self) -> int:
        """Offered items that were not written: refused when offered, or left queued."""
        return self._refused + self._abandoned

    @property
    def error(self) -> str | None:
        if self._worker_error is not None:
            return self._worker_error
        if self._timed_out and not self._writer_exited:
            return WRITER_TIMEOUT_ERROR
        return None

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        """Take no more data. The writer drains what is queued, then exits."""
        self._stop_requested.set()

    def offer(self, item: FrameItem | SensorItem) -> None:
        """Queue an item without blocking; it is dropped and counted if the queue is
        full or the writer has sealed it."""
        with self._seal_lock:
            if not self._sealed:
                try:
                    self._queue.put_nowait(item)
                    return
                except queue.Full:
                    pass
        self._refused += 1

    def finish(self, timeout_s: float) -> None:
        """Wait for the writer. If it is stuck, record that in meta.json and return."""
        self._thread.join(timeout_s)
        with self._meta_lock:
            if self._writer_exited:
                return
            self._timed_out = True
            log.error("%s: %s", WRITER_TIMEOUT_ERROR, self.session_dir)
            self._write_final_meta()

    # Writer thread.

    def _run(self) -> None:
        try:
            self._drain()
        except OSError as error:
            log.exception("recording failed: %s", self.session_dir)
            self._fail(f"recording failed: {error}")
        except Exception as error:
            log.exception("recording writer crashed: %s", self.session_dir)
            self._fail(f"recording failed: {type(error).__name__}: {error}")
        finally:
            self._seal()
            with self._meta_lock:
                self._writer_exited = True
                self._write_final_meta()
                self._done.set()
            log.info("recording finished: %s (%d frames)", self.session_dir, self.written[0])

    def _fail(self, message: str) -> None:
        self._worker_error = message
        self._stop_requested.set()

    def _drain(self) -> None:
        with (
            (self.session_dir / "frames.jsonl").open("a", buffering=1) as frame_index,
            (self.session_dir / "sensors.jsonl").open("a", buffering=1) as sensor_log,
        ):
            while True:
                try:
                    item = self._queue.get(timeout=QUEUE_POLL_S)
                except queue.Empty:
                    if self.closing and self._seal_if_empty():
                        return
                    continue
                self._write_item(item, frame_index, sensor_log)

    def _seal_if_empty(self) -> bool:
        """Seal the queue if nothing is left in it; otherwise keep draining."""
        with self._seal_lock:
            if self._queue.empty():
                self._sealed = True
            return self._sealed

    def _seal(self) -> None:
        """Refuse further offers and count anything still queued as dropped."""
        with self._seal_lock:
            self._sealed = True
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    return
                self._abandoned += 1

    def _write_item(self, item: FrameItem | SensorItem, frame_index: IO, sensor_log: IO) -> None:
        frames, sensors = self.written
        if isinstance(item, FrameItem):
            frame_index.write(write_frame_file(self.session_dir, frames + 1, item))
            self.written = (frames + 1, sensors)
        else:
            sensor_log.write(item.line)
            self.written = (frames, sensors + 1)

    def _write_final_meta(self) -> None:
        """Called with _meta_lock held."""
        frames, sensors = self.written
        final = {
            **self._meta,
            "ended_utc": self._now().isoformat(),
            "frames": frames,
            "sensors": sensors,
            "dropped": self.dropped,
            "error": self.error,
        }
        try:
            write_meta(self.session_dir, final)
        except OSError as error:
            log.exception("could not finalise recording metadata: %s", self.session_dir)
            if self._worker_error is None:
                self._worker_error = f"could not write metadata: {error}"
