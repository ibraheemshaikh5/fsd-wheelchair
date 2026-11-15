from drive_service.clock import ClockEstimate
from drive_service.protocol import Gps
from drive_service.recorder import RecordingInfo
from drive_service.state import SensorSnapshot, Stamped
from drive_service.stats import StreamStats
from drive_service.status import build_status
from helpers import FakeTime, gps_dict, make_frame

IDLE = RecordingInfo()


SYNCED = ClockEstimate(offset_ms=-5.0, rtt_ms=20.0, samples=3)


def _stamp(value, clock: FakeTime) -> Stamped:
    """A reading captured the moment it arrived, after clock sync."""
    return Stamped(
        value=value,
        received_mono=clock.mono(),
        captured_mono=clock.mono(),
    )


def test_sensors_are_ok_indoors_without_gps():
    clock = FakeTime()
    snapshot = SensorSnapshot(frame=_stamp(make_frame(), clock), clock=SYNCED, phone_connected=True)
    status = build_status(snapshot, StreamStats(), IDLE, 0, clock.mono())
    assert status["gps"] is None
    assert status["sensors_ok"] is True


def test_sensors_are_not_ok_once_the_frame_is_stale():
    clock = FakeTime()
    snapshot = SensorSnapshot(frame=_stamp(make_frame(), clock), clock=SYNCED, phone_connected=True)
    clock.advance(1.0)
    status = build_status(snapshot, StreamStats(), IDLE, 0, clock.mono())
    assert status["sensors_ok"] is False


def test_sensors_are_not_ok_without_clock_sync():
    clock = FakeTime()
    snapshot = SensorSnapshot(
        frame=_stamp(make_frame(), clock),
        gps=_stamp(Gps.model_validate(gps_dict()), clock),
        phone_connected=True,
    )
    status = build_status(snapshot, StreamStats(), IDLE, 0, clock.mono())
    assert status["frame"]["fresh"] is True
    assert status["clock_synced"] is False
    assert status["sensors_ok"] is False


def test_clock_is_not_reported_synced_once_the_phone_is_gone():
    # The hub keeps the departed phone's estimate until the next one attaches;
    # status must not call that synced.
    snapshot = SensorSnapshot(clock=SYNCED, phone_connected=False)
    status = build_status(snapshot, StreamStats(), IDLE, 0, FakeTime().mono())
    assert status["clock_synced"] is False
    assert status["clock"] == {"offset_ms": -5.0, "rtt_ms": 20.0, "samples": 3}
