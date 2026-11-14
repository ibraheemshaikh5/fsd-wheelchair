"""Where phone data lands: the latest snapshot, stream stats, and the recorder feed.

The hub holds references to immutable values and swaps them on each update.
It is only touched from the event loop thread.

The snapshot is latest-wins: every validated message replaces the previous
reading, so a burst can never leave an older reading in place of a newer one.
The flood limits only decide what is written to a recording.

Every IMU batch and GPS fix also feeds the motion estimator (speed, turn rate,
acceleration). Its latest estimate is kept in the snapshot, stamped with the
newest IMU sample it used, and each recorded IMU batch is followed by a derived
"motion" line with one row per sample.

Every handled frame gets an ``Ack`` back (the socket sends it), so the phone
can keep only a few frames in flight. Each stored frame also sets the event
``frame_arrived()`` handed out, so the model's feed sends it at once instead of
on its next tick. The phone's own link report is kept,
stamped with its receive time, and recorded like any other sensor message.

Frames can come from an external camera instead of the phone
(``external_camera=True``): a separate sender on /ws/camera, today a USB camera
on the Mac, later one on the chair's board. It has its own connection slot, hello
and clock estimate, so its frames are aged by its own clock and the phone (or
iPad) only supplies GPS, compass and motion. In that mode the phone's frames are
acked, so its flow control keeps working, but never stored.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from drive_service.clock import (
    ClockEstimate,
    ClockSample,
    add_sample,
    estimate,
    is_synced,
    to_server_ms,
)
from drive_service.config import Settings
from drive_service.motion import MotionState, motion_record, update_gps, update_imu
from drive_service.protocol import Ack, Frame, Gps, Heading, Hello, Imu, Link, Ping, Pong
from drive_service.ratelimit import RateWindow, admit
from drive_service.state import MS_PER_S, SensorSnapshot, Stamped
from drive_service.stats import StreamStats, record_frame
from drive_service.timeutil import TimeSource

log = logging.getLogger(__name__)


class RecorderSink(Protocol):
    def write_frame(self, frame: Frame, received_wall_ms: float) -> None: ...

    def write_sensor(
        self, message: Hello | Gps | Heading | Imu | Link, received_wall_ms: float
    ) -> None: ...

    def write_derived(self, record: Mapping[str, Any], received_wall_ms: float) -> None: ...


class Hub:
    def __init__(
        self,
        settings: Settings,
        recorder: RecorderSink,
        time_source: TimeSource,
        external_camera: bool = False,
    ) -> None:
        self._settings = settings
        self._external_camera = external_camera
        self._recorder = recorder
        self._time = time_source
        self._snapshot = SensorSnapshot()
        self._stats = StreamStats()
        self._clock_samples: tuple[ClockSample, ...] = ()
        self._motion = MotionState()
        self._frame_window = RateWindow()
        self._text_window = RateWindow()
        self._rejected = 0
        self._rate_limited = 0
        self._phone_lease: int | None = None
        self._lease_seq = 0
        self._camera_clock_samples: tuple[ClockSample, ...] = ()
        self._camera_lease: int | None = None
        self._camera_lease_seq = 0
        self._frame_arrived = asyncio.Event()

    @property
    def external_camera(self) -> bool:
        """True when frames come from /ws/camera and the phone's are ignored."""
        return self._external_camera

    @property
    def snapshot(self) -> SensorSnapshot:
        return self._snapshot

    @property
    def stats(self) -> StreamStats:
        return self._stats

    @property
    def rejected(self) -> int:
        return self._rejected

    @property
    def rate_limited(self) -> int:
        return self._rate_limited

    def attach_phone(self) -> int:
        """Give the phone slot to a new connection and return its lease.

        The newest connection always wins; an older one loses its lease. A new
        connection may be a different device, so its hello, clock estimate,
        motion estimate and link stats start empty. Stats and last readings stay; readings age
        out on their own.
        """
        self._lease_seq += 1
        self._phone_lease = self._lease_seq
        self._clock_samples = ()
        self._motion = MotionState()
        self._snapshot = replace(
            self._snapshot, phone_connected=True, hello=None, clock=None, motion=None, link=None
        )
        return self._lease_seq

    def holds_phone(self, lease: int) -> bool:
        return lease == self._phone_lease

    def detach_phone(self, lease: int) -> None:
        """Release the slot. Does nothing if a newer connection already took it."""
        if not self.holds_phone(lease):
            return
        self._phone_lease = None
        # Last readings are kept; they age out through the staleness checks.
        self._snapshot = replace(self._snapshot, phone_connected=False)

    def attach_camera(self) -> int:
        """Give the external camera slot to a new connection and return its lease.

        Newest wins, as for the phone. Its hello and clock start empty; the last
        frame stays and ages out.
        """
        self._camera_lease_seq += 1
        self._camera_lease = self._camera_lease_seq
        self._camera_clock_samples = ()
        self._snapshot = replace(
            self._snapshot, camera_connected=True, camera_hello=None, camera_clock=None
        )
        return self._camera_lease_seq

    def holds_camera(self, lease: int) -> bool:
        return lease == self._camera_lease

    def detach_camera(self, lease: int) -> None:
        """Release the camera slot. Does nothing if a newer connection took it."""
        if not self.holds_camera(lease):
            return
        self._camera_lease = None
        self._snapshot = replace(self._snapshot, camera_connected=False)

    def handle_camera_hello(self, hello: Hello) -> None:
        self._snapshot = replace(self._snapshot, camera_hello=hello)

    def handle_camera_ping(self, ping: Ping) -> Pong:
        """Clock sync for the external camera, kept apart from the phone's."""
        wall_ms = self._time.wall_ms()
        if ping.rtt_ms is not None and ping.offset_ms is not None:
            self._camera_clock_samples = add_sample(
                self._camera_clock_samples,
                ClockSample(rtt_ms=ping.rtt_ms, offset_ms=ping.offset_ms),
                self._settings.clock_window,
            )
            camera_clock = estimate(self._camera_clock_samples)
            self._snapshot = replace(self._snapshot, camera_clock=camera_clock)
        return Pong(t=ping.t, server_t=wall_ms)

    def handle_camera_frame(self, frame: Frame) -> Ack:
        """Store a frame from the external camera, aged by the camera's own clock."""
        now = self._time.mono()
        wall_ms = self._time.wall_ms()
        clock = self._snapshot.camera_clock
        captured, future_ms = _capture_time(frame.header.t, now, wall_ms, clock, self._settings)
        if future_ms is not None:
            log.warning(
                "camera frame is %.0f ms in the future; camera clock dropped until its next ping",
                future_ms,
            )
            self._camera_clock_samples = ()
            self._snapshot = replace(self._snapshot, camera_clock=None)
            clock = None
        return self._store_frame(frame, now, wall_ms, captured, clock)

    def frame_arrived(self) -> asyncio.Event:
        """An event set when the next frame is stored. Ask again after it fires."""
        return self._frame_arrived

    def note_rejected(self) -> None:
        self._rejected += 1

    def ack_frame(self, seq: int) -> Ack:
        """The ack for frame ``seq``, stamped now. Also used for frames that were refused."""
        return Ack(seq=seq, t=self._time.wall_ms())

    def handle_frame(self, frame: Frame) -> Ack:
        """Store a validated frame, and record it unless the frame flood limit is hit.

        Returns the ack to send, whether or not the frame was recorded. With an
        external camera the phone's frame is only acked.
        """
        now = self._time.mono()
        wall_ms = self._time.wall_ms()
        if self._external_camera:
            return Ack(seq=frame.header.seq, t=wall_ms)
        captured = self._captured_mono(frame.header.t, now, wall_ms)
        return self._store_frame(frame, now, wall_ms, captured, self._snapshot.clock)

    def _store_frame(
        self,
        frame: Frame,
        now: float,
        wall_ms: float,
        captured: float | None,
        clock: ClockEstimate | None,
    ) -> Ack:
        latency_ms = None if clock is None else wall_ms - to_server_ms(frame.header.t, clock)
        self._snapshot = replace(self._snapshot, frame=Stamped(frame, now, captured))
        arrived, self._frame_arrived = self._frame_arrived, asyncio.Event()
        arrived.set()
        self._stats = record_frame(
            self._stats, frame.header.seq, now, latency_ms, self._settings.stats_window
        )
        allowed, self._frame_window = self._admit(
            self._frame_window, now, self._settings.max_frame_rate_hz
        )
        if allowed:
            self._recorder.write_frame(frame, wall_ms)
        return Ack(seq=frame.header.seq, t=wall_ms)

    def handle_message(self, message: Hello | Gps | Heading | Imu | Ping | Link) -> Pong | None:
        """Apply a validated text message. Returns a reply to send, if any."""
        now = self._time.mono()
        wall_ms = self._time.wall_ms()
        if isinstance(message, Ping):
            return self._handle_ping(message, wall_ms)
        derived = self._store(message, now, wall_ms)
        allowed, self._text_window = self._admit(
            self._text_window, now, self._settings.max_text_rate_hz
        )
        if allowed:
            self._recorder.write_sensor(message, wall_ms)
            # Only beside a recorded batch, so motion lines match imu lines 1:1.
            if derived is not None:
                self._recorder.write_derived(derived, wall_ms)
        return None

    def _store(
        self, message: Hello | Gps | Heading | Imu | Link, now: float, wall_ms: float
    ) -> dict[str, Any] | None:
        """Update the snapshot. Returns a derived line to record, if any."""
        if isinstance(message, Imu):
            return self._store_imu(message, now, wall_ms)
        if isinstance(message, Hello):
            self._snapshot = replace(self._snapshot, hello=message)
        elif isinstance(message, Gps):
            # A resent or cached fix is as old as the fix itself.
            captured = self._captured_mono(min(message.fix_t, message.t), now, wall_ms)
            self._snapshot = replace(self._snapshot, gps=Stamped(message, now, captured))
            self._store_gps_speed(message)
        elif isinstance(message, Heading):
            captured = self._captured_mono(message.t, now, wall_ms)
            self._snapshot = replace(self._snapshot, heading=Stamped(message, now, captured))
        elif isinstance(message, Link):
            self._snapshot = replace(self._snapshot, link=Stamped(message, now))
        return None

    def _store_imu(self, batch: Imu, now: float, wall_ms: float) -> dict[str, Any]:
        hello = self._snapshot.hello
        # Without the marker the page is older and wrote rotation as [y, z, x].
        legacy_axes = hello is None or hello.imu_axes != "device_xyz"
        self._motion, result = update_imu(self._motion, batch, legacy_axes)
        if result.newest_t_ms is not None:
            captured = self._captured_mono(result.newest_t_ms, now, wall_ms)
            motion = Stamped(self._motion.estimate, now, captured)
            self._snapshot = replace(self._snapshot, motion=motion)
        return motion_record(batch, self._motion, result)

    def _store_gps_speed(self, fix: Gps) -> None:
        """Blend the fix into the motion estimate; its age stays the IMU sample's."""
        self._motion = update_gps(self._motion, fix)
        current = self._snapshot.motion
        if current is not None:
            motion = replace(current, value=self._motion.estimate)
            self._snapshot = replace(self._snapshot, motion=motion)

    def _admit(self, window: RateWindow, now: float, limit: int) -> tuple[bool, RateWindow]:
        allowed, updated = admit(window, now, limit)
        if not allowed:
            self._rate_limited += 1
        return allowed, updated

    def _captured_mono(self, phone_t: float, now_mono: float, wall_ms: float) -> float | None:
        """Service monotonic time a reading stamped ``phone_t`` was captured.

        ``now_mono`` and ``wall_ms`` are read at the same moment of receipt, so a
        later step of the server's wall clock has no effect. None without a
        trusted clock estimate, which makes the reading never fresh.
        """
        captured, future_ms = _capture_time(
            phone_t, now_mono, wall_ms, self._snapshot.clock, self._settings
        )
        if future_ms is not None:
            self._forget_clock(future_ms)
        return captured

    def _forget_clock(self, future_ms: float) -> None:
        """A reading claims to come from the future, so the offset is wrong."""
        log.warning(
            "phone reading is %.0f ms in the future; clock estimate dropped until the next ping",
            future_ms,
        )
        self._clock_samples = ()
        self._snapshot = replace(self._snapshot, clock=None)

    def _handle_ping(self, ping: Ping, wall_ms: float) -> Pong:
        if ping.rtt_ms is not None and ping.offset_ms is not None:
            self._clock_samples = add_sample(
                self._clock_samples,
                ClockSample(rtt_ms=ping.rtt_ms, offset_ms=ping.offset_ms),
                self._settings.clock_window,
            )
            self._snapshot = replace(self._snapshot, clock=estimate(self._clock_samples))
        return Pong(t=ping.t, server_t=wall_ms)


def _capture_time(
    device_t: float,
    now_mono: float,
    wall_ms: float,
    clock: ClockEstimate | None,
    settings: Settings,
) -> tuple[float | None, float | None]:
    """(service monotonic capture time, ms in the future) for a reading stamped ``device_t``.

    The capture time is None without a trusted clock estimate, which makes the
    reading never fresh. The second value is set when the reading claims to come
    from further in the future than the allowed skew: the offset is wrong and the
    caller must drop it.
    """
    if clock is None or not is_synced(clock, settings.max_clock_rtt_ms):
        return None, None
    # Half the round trip is the worst-case error of a min-RTT offset; adding
    # it can only make the reading look older.
    transit_ms = wall_ms - to_server_ms(device_t, clock) + clock.rtt_ms / 2
    if transit_ms < -settings.max_clock_skew_ms:
        return None, -transit_ms
    return now_mono - max(0.0, transit_ms) / MS_PER_S, None
