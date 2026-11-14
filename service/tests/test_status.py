import json

import pytest

from drive_service.clock import ClockEstimate
from drive_service.config import Settings
from drive_service.declination import DEFAULT_CACHE
from drive_service.destination import Destination
from drive_service.protocol import Gps, Heading, Hello
from drive_service.recorder import RecordingInfo
from drive_service.state import SensorSnapshot, Stamped
from drive_service.stats import StreamStats, record_frame
from drive_service.status import build_status
from helpers import FakeTime, gps_dict, heading_dict, hello_dict, make_frame

IDLE = RecordingInfo()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(record_dir=tmp_path, web_dist=tmp_path / "missing")


@pytest.fixture(autouse=True)
def _fresh_declination_cache():
    DEFAULT_CACHE.clear()
    yield
    DEFAULT_CACHE.clear()


SYNCED = ClockEstimate(offset_ms=-5.0, rtt_ms=20.0, samples=3)


def _stamp(value, clock: FakeTime) -> Stamped:
    """A reading captured the moment it arrived, after clock sync."""
    return Stamped(
        value=value,
        received_mono=clock.mono(),
        captured_mono=clock.mono(),
    )


def test_empty_snapshot_reports_nothing_fresh(settings):
    status = build_status(SensorSnapshot(), StreamStats(), IDLE, 0, 0, FakeTime().mono(), settings)
    assert status["type"] == "status"
    assert status["phone_connected"] is False
    assert status["frame"] is None
    assert status["gps"] is None
    assert status["heading"] is None
    assert status["sensors_ok"] is False
    assert status["clock_synced"] is False
    assert status["recording"]["active"] is False


def test_full_snapshot_is_reported_and_json_serialisable(settings):
    clock = FakeTime()
    snapshot = SensorSnapshot(
        hello=Hello.model_validate(hello_dict()),
        frame=_stamp(make_frame(seq=9), clock),
        gps=_stamp(Gps.model_validate(gps_dict(speed_mps=2.0, course_deg=180.0)), clock),
        heading=_stamp(Heading.model_validate(heading_dict(magnetic_deg=170.0)), clock),
        clock=SYNCED,
        phone_connected=True,
    )
    stats = record_frame(
        StreamStats(), seq=9, received_mono=clock.mono(), latency_ms=60.0, window=10
    )
    clock.advance(0.1)

    status = build_status(snapshot, stats, IDLE, 2, 1, clock.mono(), settings)

    json.dumps(status)
    assert status["phone_connected"] is True
    assert status["sensors_ok"] is True
    assert status["clock_synced"] is True
    assert status["frame"]["seq"] == 9
    assert status["frame"]["fresh"] is True
    assert status["frame"]["age_ms"] == pytest.approx(100.0)
    assert status["gps"]["lat"] == pytest.approx(38.9869)
    assert status["heading"] == {
        "deg": 180.0,
        "source": "gps_course",
        "reference": "true",
        "declination_deg": pytest.approx(-10.8, abs=0.5),
        "compass_deg": 170.0,
        "compass_accuracy_deg": 10.0,
        "compass_age_ms": pytest.approx(100.0),
        "compass_fresh": True,
    }
    assert status["clock"] == {"offset_ms": -5.0, "rtt_ms": 20.0, "samples": 3}
    assert status["stats"]["latency_p50_ms"] == 60.0
    assert status["stats"]["rejected"] == 2
    assert status["stats"]["rate_limited"] == 1
    assert status["camera"]["label"] == "Back Ultra Wide Camera"


def test_stale_readings_are_flagged_and_not_used_for_heading(settings):
    clock = FakeTime()
    snapshot = SensorSnapshot(
        frame=_stamp(make_frame(), clock),
        gps=_stamp(Gps.model_validate(gps_dict(speed_mps=2.0, course_deg=180.0)), clock),
        heading=_stamp(Heading.model_validate(heading_dict()), clock),
        clock=SYNCED,
        phone_connected=True,
    )
    # Past every limit, GPS being the longest.
    clock.advance(settings.gps_stale_ms / 1000 + 1.0)

    status = build_status(snapshot, StreamStats(), IDLE, 0, 0, clock.mono(), settings)

    assert status["frame"]["fresh"] is False
    assert status["gps"]["fresh"] is False
    assert status["heading"]["deg"] is None
    assert status["heading"]["reference"] is None
    assert status["heading"]["declination_deg"] is None
    assert status["heading"]["compass_fresh"] is False
    assert status["sensors_ok"] is False


def test_recording_info_is_included(settings):
    info = RecordingInfo(active=True, session="20261114T030000Z", frames=12, sensors=40)
    status = build_status(SensorSnapshot(), StreamStats(), info, 0, 0, 0.0, settings)
    assert status["recording"] == {
        "active": True,
        "session": "20261114T030000Z",
        "frames": 12,
        "sensors": 40,
        "dropped": 0,
        "error": None,
        "finishing": False,
        "end_reason": None,
    }


def test_recording_end_reason_and_finishing_are_included(settings):
    info = RecordingInfo(
        session="20261114T030000Z", frames=2, finishing=True, end_reason="frame limit reached (2)"
    )
    status = build_status(SensorSnapshot(), StreamStats(), info, 0, 0, 0.0, settings)
    assert status["recording"]["active"] is False
    assert status["recording"]["finishing"] is True
    assert status["recording"]["end_reason"] == "frame limit reached (2)"
    assert status["recording"]["error"] is None


def test_sensors_are_not_ok_without_clock_sync(settings):
    clock = FakeTime()
    snapshot = SensorSnapshot(
        frame=_stamp(make_frame(), clock),
        gps=_stamp(Gps.model_validate(gps_dict()), clock),
        phone_connected=True,
    )
    status = build_status(snapshot, StreamStats(), IDLE, 0, 0, clock.mono(), settings)
    assert status["frame"]["fresh"] is True
    assert status["clock_synced"] is False
    assert status["sensors_ok"] is False


def test_clock_is_not_reported_synced_once_the_phone_is_gone(settings):
    # The hub keeps the departed phone's estimate until the next one attaches;
    # status must not call that synced.
    snapshot = SensorSnapshot(clock=SYNCED, phone_connected=False)
    status = build_status(snapshot, StreamStats(), IDLE, 0, 0, FakeTime().mono(), settings)
    assert status["clock_synced"] is False
    assert status["clock"] == {"offset_ms": -5.0, "rtt_ms": 20.0, "samples": 3}


def test_fps_is_reported_over_recent_frames_only(settings):
    stats = record_frame(StreamStats(), seq=0, received_mono=0.0, latency_ms=None, window=10)
    stats = record_frame(stats, seq=1, received_mono=0.1, latency_ms=None, window=10)
    status = build_status(SensorSnapshot(), stats, IDLE, 0, 0, 60.0, settings)
    assert status["stats"]["fps"] == 0.0


def _stopped_with_compass(clock: FakeTime, gps: Gps | None) -> SensorSnapshot:
    return SensorSnapshot(
        gps=_stamp(gps, clock) if gps else None,
        heading=_stamp(Heading.model_validate(heading_dict(magnetic_deg=170.0)), clock),
        clock=SYNCED,
        phone_connected=True,
    )


def test_compass_heading_is_true_north_with_configured_declination(tmp_path):
    settings = Settings(record_dir=tmp_path, magnetic_declination_deg=-11.0)
    clock = FakeTime()
    status = build_status(
        _stopped_with_compass(clock, None), StreamStats(), IDLE, 0, 0, clock.mono(), settings
    )
    heading = status["heading"]
    assert heading["deg"] == 159.0
    assert heading["source"] == "compass"
    assert heading["reference"] == "true"
    assert heading["declination_deg"] == -11.0
    assert heading["compass_deg"] == 170.0


def test_compass_heading_uses_declination_from_the_gps_fix(settings):
    clock = FakeTime()
    stopped_fix = Gps.model_validate(gps_dict(speed_mps=0.0, course_deg=None))
    status = build_status(
        _stopped_with_compass(clock, stopped_fix), StreamStats(), IDLE, 0, 0, clock.mono(), settings
    )
    heading = status["heading"]
    assert heading["reference"] == "true"
    assert -12.0 < heading["declination_deg"] < -10.0
    assert heading["deg"] == pytest.approx(170.0 + heading["declination_deg"])
    assert heading["compass_deg"] == 170.0


def test_compass_heading_stays_magnetic_without_any_declination(settings):
    clock = FakeTime()
    status = build_status(
        _stopped_with_compass(clock, None), StreamStats(), IDLE, 0, 0, clock.mono(), settings
    )
    heading = status["heading"]
    assert heading["deg"] == 170.0
    assert heading["reference"] == "magnetic"
    assert heading["declination_deg"] is None


def test_stale_gps_fix_does_not_set_declination(settings):
    clock = FakeTime()
    snapshot = _stopped_with_compass(clock, Gps.model_validate(gps_dict(speed_mps=0.0)))
    clock.advance(settings.gps_stale_ms / 1000.0 + 0.5)
    snapshot = SensorSnapshot(
        gps=snapshot.gps,
        heading=_stamp(Heading.model_validate(heading_dict(magnetic_deg=170.0)), clock),
        clock=SYNCED,
        phone_connected=True,
    )
    status = build_status(snapshot, StreamStats(), IDLE, 0, 0, clock.mono(), settings)
    assert status["heading"]["reference"] == "magnetic"
    assert status["heading"]["declination_deg"] is None


def test_destination_is_measured_from_a_fresh_fix(settings):
    clock = FakeTime()
    snapshot = SensorSnapshot(
        gps=_stamp(Gps.model_validate(gps_dict(lat=38.9869, lon=-76.9426)), clock),
        clock=SYNCED,
        phone_connected=True,
    )
    target = Destination(lat=38.9879, lon=-76.9426)

    status = build_status(
        snapshot, StreamStats(), IDLE, 0, 0, clock.mono(), settings, destination=target
    )

    assert status["destination"]["distance_m"] == pytest.approx(111.2, abs=0.5)
    assert status["destination"]["bearing_deg"] == pytest.approx(0.0, abs=0.1)
    json.dumps(status)


def test_destination_has_no_distance_once_the_fix_is_stale(settings):
    clock = FakeTime()
    snapshot = SensorSnapshot(
        gps=_stamp(Gps.model_validate(gps_dict()), clock), clock=SYNCED, phone_connected=True
    )
    clock.advance(settings.gps_stale_ms / 1000 + 0.1)
    target = Destination(lat=38.99, lon=-76.94)

    status = build_status(
        snapshot, StreamStats(), IDLE, 0, 0, clock.mono(), settings, destination=target
    )

    assert status["destination"] == {
        "lat": 38.99,
        "lon": -76.94,
        "distance_m": None,
        "bearing_deg": None,
    }


def test_no_destination_by_default(settings):
    status = build_status(SensorSnapshot(), StreamStats(), IDLE, 0, 0, 0.0, settings)
    assert status["destination"] is None
