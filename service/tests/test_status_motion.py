"""The status "motion" block: speed, turn rate and acceleration with age and freshness."""

import json

import pytest

from drive_service.config import Settings
from drive_service.motion import MotionEstimate
from drive_service.recorder import RecordingInfo
from drive_service.state import SensorSnapshot, Stamped
from drive_service.stats import StreamStats
from drive_service.status import build_status
from helpers import FakeTime

IDLE = RecordingInfo()
ESTIMATE = MotionEstimate(
    speed_mps=1.3,
    speed_source="imu",
    yaw_rate_dps=12.5,
    accel_fwd_mps2=0.4,
    accel_lat_mps2=-0.2,
)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(record_dir=tmp_path, web_dist=tmp_path / "missing")


def _status(snapshot: SensorSnapshot, now: float, settings: Settings) -> dict:
    return build_status(snapshot, StreamStats(), IDLE, 0, 0, now, settings)


def test_motion_is_null_before_any_imu(settings):
    assert _status(SensorSnapshot(), FakeTime().mono(), settings)["motion"] is None


def test_fresh_motion_is_reported_in_full(settings):
    clock = FakeTime()
    snapshot = SensorSnapshot(motion=Stamped(ESTIMATE, clock.mono(), clock.mono() - 0.1))
    status = _status(snapshot, clock.mono(), settings)
    assert status["motion"] == {
        "speed_mps": 1.3,
        "speed_source": "imu",
        "yaw_rate_dps": 12.5,
        "accel_fwd_mps2": 0.4,
        "accel_lat_mps2": -0.2,
        "age_ms": pytest.approx(100.0),
        "fresh": True,
    }
    json.dumps(status)


def test_motion_older_than_250_ms_is_not_fresh(settings):
    clock = FakeTime()
    snapshot = SensorSnapshot(motion=Stamped(ESTIMATE, clock.mono(), clock.mono()))
    clock.advance(0.26)
    motion = _status(snapshot, clock.mono(), settings)["motion"]
    assert motion["fresh"] is False
    assert motion["age_ms"] == pytest.approx(260.0)


def test_motion_without_capture_time_is_never_fresh(settings):
    clock = FakeTime()
    snapshot = SensorSnapshot(motion=Stamped(ESTIMATE, clock.mono(), None))
    motion = _status(snapshot, clock.mono(), settings)["motion"]
    assert motion["fresh"] is False
    assert motion["age_ms"] == pytest.approx(0.0)


def test_unknown_values_stay_null(settings):
    clock = FakeTime()
    snapshot = SensorSnapshot(motion=Stamped(MotionEstimate(), clock.mono(), clock.mono()))
    motion = _status(snapshot, clock.mono(), settings)["motion"]
    assert motion["speed_mps"] is None
    assert motion["speed_source"] is None
    assert motion["yaw_rate_dps"] is None
