"""Where phone data lands: the latest snapshot, stream stats, and the recorder feed.

The hub holds references to immutable values and swaps them on each update.
It is only touched from the event loop thread.

The snapshot is latest-wins: every validated message replaces the previous
reading, so a burst can never leave an older reading in place of a newer one.
The flood limits only decide what is written to a recording.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Protocol

from drive_service.clock import ClockSample, add_sample, estimate, is_synced, to_server_ms
from drive_service.config import Settings
from drive_service.protocol import Frame, Gps, Heading, Hello, Imu, Ping, Pong
from drive_service.ratelimit import RateWindow, admit
from drive_service.state import MS_PER_S, SensorSnapshot, Stamped
from drive_service.stats import StreamStats, record_frame
from drive_service.timeutil import TimeSource

log = logging.getLogger(__name__)


class RecorderSink(Protocol):
    def write_frame(self, frame: Frame, received_wall_ms: float) -> None: ...

    def write_sensor(
        self, message: Hello | Gps | Heading | Imu, received_wall_ms: float
    ) -> None: ...


class Hub:
    def __init__(self, settings: Settings, recorder: RecorderSink, time_source: TimeSource) -> None:
        self._settings = settings
        self._recorder = recorder
        self._time = time_source
        self._snapshot = SensorSnapshot()
        self._stats = StreamStats()
        self._clock_samples: tuple[ClockSample, ...] = ()
        self._frame_window = RateWindow()
        self._text_window = RateWindow()
        self._rejected = 0
        self._rate_limited = 0
        self._phone_lease: int | None = None
        self._lease_seq = 0

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
        connection may be a different device, so its hello and clock estimate
        start empty. Stats and last readings stay; readings age out on their own.
        """
        self._lease_seq += 1
        self._phone_lease = self._lease_seq
        self._clock_samples = ()
        self._snapshot = replace(self._snapshot, phone_connected=True, hello=None, clock=None)
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

    def note_rejected(self) -> None:
        self._rejected += 1

    def handle_frame(self, frame: Frame) -> None:
        """Store a validated frame, and record it unless the frame flood limit is hit."""
        now = self._time.mono()
        wall_ms = self._time.wall_ms()
        captured = self._captured_mono(frame.header.t, now, wall_ms)
        clock = self._snapshot.clock
        latency_ms = None if clock is None else wall_ms - to_server_ms(frame.header.t, clock)
        self._snapshot = replace(self._snapshot, frame=Stamped(frame, now, captured))
        self._stats = record_frame(
            self._stats, frame.header.seq, now, latency_ms, self._settings.stats_window
        )
        allowed, self._frame_window = self._admit(
            self._frame_window, now, self._settings.max_frame_rate_hz
        )
        if allowed:
            self._recorder.write_frame(frame, wall_ms)

    def handle_message(self, message: Hello | Gps | Heading | Imu | Ping) -> Pong | None:
        """Apply a validated text message. Returns a reply to send, if any."""
        now = self._time.mono()
        wall_ms = self._time.wall_ms()
        if isinstance(message, Ping):
            return self._handle_ping(message, wall_ms)
        self._store(message, now, wall_ms)
        allowed, self._text_window = self._admit(
            self._text_window, now, self._settings.max_text_rate_hz
        )
        if allowed:
            self._recorder.write_sensor(message, wall_ms)
        return None

    def _store(self, message: Hello | Gps | Heading | Imu, now: float, wall_ms: float) -> None:
        if isinstance(message, Hello):
            self._snapshot = replace(self._snapshot, hello=message)
        elif isinstance(message, Gps):
            # A resent or cached fix is as old as the fix itself.
            captured = self._captured_mono(min(message.fix_t, message.t), now, wall_ms)
            self._snapshot = replace(self._snapshot, gps=Stamped(message, now, captured))
        elif isinstance(message, Heading):
            captured = self._captured_mono(message.t, now, wall_ms)
            self._snapshot = replace(self._snapshot, heading=Stamped(message, now, captured))
        # IMU batches are only recorded, not kept in the snapshot.

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
        clock = self._snapshot.clock
        if clock is None or not is_synced(clock, self._settings.max_clock_rtt_ms):
            return None
        # Half the round trip is the worst-case error of a min-RTT offset; adding
        # it can only make the reading look older.
        transit_ms = wall_ms - to_server_ms(phone_t, clock) + clock.rtt_ms / 2
        if transit_ms < -self._settings.max_clock_skew_ms:
            self._forget_clock(transit_ms)
            return None
        return now_mono - max(0.0, transit_ms) / MS_PER_S

    def _forget_clock(self, transit_ms: float) -> None:
        """A reading claims to come from the future, so the offset is wrong."""
        log.warning(
            "phone reading is %.0f ms in the future; clock estimate dropped until the next ping",
            -transit_ms,
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
