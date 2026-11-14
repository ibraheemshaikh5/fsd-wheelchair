"""The fake phone's messages must pass the real service's validation end to end."""

import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from drive_service.app import create_app
from drive_service.config import Settings
from scripts import fake_phone

NOW_MS = 1_700_000_000_000.0


def _distance_m(a: fake_phone.Pose, b: fake_phone.Pose) -> float:
    north = (b.lat - a.lat) * fake_phone.METERS_PER_DEG_LAT
    east = (
        (b.lon - a.lon)
        * fake_phone.METERS_PER_DEG_LAT
        * math.cos(math.radians(fake_phone.START_LAT))
    )
    return math.hypot(north, east)


def test_pose_moves_at_walking_speed_and_heads_north_at_start():
    start = fake_phone.pose_at(0.0)
    later = fake_phone.pose_at(1.0)
    assert _distance_m(start, later) == pytest.approx(fake_phone.WALK_SPEED_MPS, rel=0.02)
    assert start.course_deg == pytest.approx(0.0)
    assert 0.0 <= later.course_deg < 360.0


def test_course_matches_direction_of_travel_a_quarter_of_the_way_round():
    quarter_s = (math.pi / 2) * fake_phone.LOOP_RADIUS_M / fake_phone.WALK_SPEED_MPS
    here = fake_phone.pose_at(quarter_s)
    ahead = fake_phone.pose_at(quarter_s + 0.5)
    assert here.course_deg == pytest.approx(270.0)
    assert ahead.lon < here.lon  # actually moving west


def test_schedule_sends_gps_once_per_second():
    ticks = [due for (_, due), _ in zip(fake_phone.schedule(10.0), range(20), strict=False)]
    assert sum("gps" in due for due in ticks) == 2
    assert all("frame" in due for due in ticks)
    assert sum("heading" in due for due in ticks) == 20


def test_jpeg_is_a_real_image():
    data = fake_phone.make_jpeg(3, 64, 48)
    assert data.startswith(b"\xff\xd8\xff")
    assert data.endswith(b"\xff\xd9")


def test_fake_phone_messages_are_accepted_by_the_service(tmp_path: Path):
    settings = Settings(
        record_dir=tmp_path / "rec",
        web_dist=tmp_path / "missing",
        allowed_hosts=("testserver",),
        recorder_min_free_bytes=0,
    )
    pose = fake_phone.pose_at(2.0)
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect("/ws/phone") as phone:
            phone.send_text(fake_phone.hello(NOW_MS, 64, 48, 10.0))
            phone.send_text(fake_phone.ping(NOW_MS, 12.0, -3.0))
            assert json.loads(phone.receive_text())["type"] == "pong"
            # Recording needs a connected phone that has said hello.
            assert client.post("/api/recording/start").status_code == 200
            phone.send_text(fake_phone.gps(NOW_MS, pose))
            phone.send_text(fake_phone.heading(NOW_MS, pose))
            phone.send_text(fake_phone.imu(NOW_MS))
            phone.send_bytes(fake_phone.frame(0, NOW_MS, 64, 48))
            assert json.loads(phone.receive_text())["type"] == "ack"
            phone.send_text(fake_phone.ping(NOW_MS, 12.0, -3.0))
            assert json.loads(phone.receive_text())["type"] == "pong"
            status = client.get("/api/status").json()["data"]
        stopped = client.post("/api/recording/stop").json()["data"]

    assert status["stats"]["rejected"] == 0
    assert status["gps"]["lat"] == pytest.approx(pose.lat)
    assert status["frame"]["w"] == 64
    assert status["clock"]["offset_ms"] == -3.0
    assert stopped["frames"] == 1
    assert stopped["sensors"] == 4  # gps, heading, imu, motion (hello came before the start)


def test_fake_imu_looks_like_an_upright_mounted_iphone():
    # iOS reports gravity pointing down, so an upright phone (top up) reads -g on y,
    # and the motion estimator can find a horizontal forward axis.
    from drive_service.motion import MotionState, update_imu
    from drive_service.protocol import Imu

    batch = Imu.model_validate(json.loads(fake_phone.imu(NOW_MS)))
    assert all(s[2] == pytest.approx(-fake_phone.GRAVITY) for s in batch.samples)
    _, result = update_imu(MotionState(), batch, legacy_axes=False)
    assert result.rows[-1].accel_fwd_mps2 == pytest.approx(0.0, abs=0.01)
    assert result.rows[-1].yaw_rate_dps == pytest.approx(0.0, abs=0.01)


def test_fake_hello_declares_device_axes():
    assert json.loads(fake_phone.hello(NOW_MS, 640, 360, 60.0))["imu_axes"] == "device_xyz"
