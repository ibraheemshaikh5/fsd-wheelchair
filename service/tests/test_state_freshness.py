"""Readings are aged by the older of receive time and capture time, and fail closed."""

import pytest

from drive_service import config
from drive_service.hub import Hub
from drive_service.protocol import Gps, Heading, Ping
from drive_service.recorder import RecordingInfo
from drive_service.state import Stamped, age_ms, is_fresh
from drive_service.status import build_status
from helpers import FakeTime, NullRecorder, gps_dict, heading_dict, make_frame, ping_dict

RTT_MS = 20.0
HALF_RTT_MS = RTT_MS / 2


@pytest.fixture
def clock() -> FakeTime:
    return FakeTime()


@pytest.fixture
def hub(clock) -> Hub:
    hub = Hub(NullRecorder(), clock)
    hub.attach_phone()
    return hub


def _sync(hub: Hub, clock: FakeTime, offset_ms: float = 0.0, rtt_ms: float = RTT_MS) -> None:
    hub.handle_message(
        Ping.model_validate(ping_dict(t=clock.wall_ms(), rtt_ms=rtt_ms, offset_ms=offset_ms))
    )


def _heading(t: float) -> Heading:
    return Heading.model_validate(heading_dict(t=t))


def test_frame_is_aged_by_capture_time(hub, clock):
    _sync(hub, clock)
    hub.handle_frame(make_frame(t=clock.wall_ms() - 3000.0))
    frame = hub.snapshot.frame
    assert not is_fresh(frame, config.FRAME_STALE_MS, clock.mono())
    assert age_ms(frame, clock.mono()) == pytest.approx(3000.0 + HALF_RTT_MS)


def test_offset_converts_phone_time(hub, clock):
    # Phone clock 5 s ahead; the frame was captured 100 ms ago on the server's clock.
    _sync(hub, clock, offset_ms=-5000.0)
    hub.handle_frame(make_frame(t=clock.wall_ms() + 5000.0 - 100.0))
    assert age_ms(hub.snapshot.frame, clock.mono()) == pytest.approx(100.0 + HALF_RTT_MS)
    assert is_fresh(hub.snapshot.frame, config.FRAME_STALE_MS, clock.mono())


def test_heading_delayed_in_transit_is_stale(hub, clock):
    _sync(hub, clock)
    hub.handle_message(_heading(clock.wall_ms() - 3000.0))
    assert not is_fresh(hub.snapshot.heading, config.HEADING_STALE_MS, clock.mono())


def test_gps_is_aged_by_fix_t(hub, clock):
    _sync(hub, clock)
    now = clock.wall_ms()
    # Received just now, but computed almost a full limit ago.
    fix_age_ms = config.GPS_STALE_MS - HALF_RTT_MS - 1.0
    hub.handle_message(Gps.model_validate(gps_dict(t=now, fix_t=now - fix_age_ms)))
    assert is_fresh(hub.snapshot.gps, config.GPS_STALE_MS, clock.mono())
    clock.advance(0.1)
    assert not is_fresh(hub.snapshot.gps, config.GPS_STALE_MS, clock.mono())


def test_gps_stays_fresh_between_stationary_iphone_fixes(hub, clock):
    # Measured on an iPhone at rest: Safari reports a new fix only about every 5 s.
    # A stopped chair must not lose GPS between them, or it could never start moving.
    _sync(hub, clock)
    now = clock.wall_ms()
    hub.handle_message(Gps.model_validate(gps_dict(t=now, fix_t=now)))
    clock.advance(6.0)
    assert is_fresh(hub.snapshot.gps, config.GPS_STALE_MS, clock.mono())
    clock.advance(5.0)
    assert not is_fresh(hub.snapshot.gps, config.GPS_STALE_MS, clock.mono())


def test_gps_uses_message_time_when_older_than_fix_t(hub, clock):
    _sync(hub, clock)
    now = clock.wall_ms()
    hub.handle_message(Gps.model_validate(gps_dict(t=now - 2000.0, fix_t=now)))
    assert age_ms(hub.snapshot.gps, clock.mono()) == pytest.approx(2000.0 + HALF_RTT_MS)


def test_nothing_is_fresh_before_clock_sync(hub, clock):
    now = clock.wall_ms()
    hub.handle_frame(make_frame(t=now))
    hub.handle_message(Gps.model_validate(gps_dict(t=now, fix_t=now)))
    hub.handle_message(_heading(now))
    snapshot = hub.snapshot
    assert not is_fresh(snapshot.frame, config.FRAME_STALE_MS, clock.mono())
    assert not is_fresh(snapshot.gps, config.GPS_STALE_MS, clock.mono())
    assert not is_fresh(snapshot.heading, config.HEADING_STALE_MS, clock.mono())
    # The age is still shown, measured from receipt.
    assert age_ms(snapshot.frame, clock.mono()) == 0.0
    status = build_status(snapshot, hub.stats, RecordingInfo(), 0, clock.mono())
    assert status["clock_synced"] is False
    assert status["sensors_ok"] is False


def test_phone_clock_ahead_never_looks_fresher_than_receipt(hub, clock):
    _sync(hub, clock)
    hub.handle_message(_heading(clock.wall_ms() + 100.0))
    assert hub.snapshot.clock is not None
    assert age_ms(hub.snapshot.heading, clock.mono()) == 0.0
    clock.advance(1.01)
    assert not is_fresh(hub.snapshot.heading, config.HEADING_STALE_MS, clock.mono())


def test_reading_from_the_future_invalidates_clock(hub, clock):
    _sync(hub, clock)
    hub.handle_message(_heading(clock.wall_ms() + 5000.0))
    assert hub.snapshot.clock is None
    assert not is_fresh(hub.snapshot.heading, config.HEADING_STALE_MS, clock.mono())
    # The next measured ping restores the estimate from scratch.
    _sync(hub, clock, rtt_ms=60.0)
    assert hub.snapshot.clock.rtt_ms == 60.0
    assert hub.snapshot.clock.samples == 1


def test_clock_with_long_round_trip_is_not_trusted(hub, clock):
    _sync(hub, clock, rtt_ms=config.MAX_CLOCK_RTT_MS + 1.0)
    hub.handle_frame(make_frame(t=clock.wall_ms()))
    assert not is_fresh(hub.snapshot.frame, config.FRAME_STALE_MS, clock.mono())
    status = build_status(hub.snapshot, hub.stats, RecordingInfo(), 0, clock.mono())
    assert status["clock_synced"] is False


def test_unsynced_reading_has_no_capture_time(clock):
    reading = Stamped(value=None, received_mono=clock.mono())
    assert reading.captured_mono is None
    assert is_fresh(reading, 500.0, clock.mono()) is False


def test_synced_reading_is_fresh_within_limit(clock):
    reading = Stamped(
        value=None,
        received_mono=clock.mono(),
        captured_mono=clock.mono() - 0.2,
    )
    assert age_ms(reading, clock.mono()) == pytest.approx(200.0)
    assert is_fresh(reading, 500.0, clock.mono()) is True
    assert is_fresh(reading, 100.0, clock.mono()) is False
