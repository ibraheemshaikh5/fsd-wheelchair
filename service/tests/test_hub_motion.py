"""The hub feeds IMU and GPS into the motion estimator and records its output."""

from pathlib import Path

import pytest

from drive_service.config import Settings
from drive_service.hub import Hub
from drive_service.protocol import Gps, Hello, Imu, Ping
from drive_service.state import is_fresh
from helpers import FakeTime, gps_dict, hello_dict, ping_dict

G = 9.80665
SAMPLE_MS = 16.0
MOTION_STALE_MS = 250.0


class SpyRecorder:
    def __init__(self) -> None:
        self.sensors = []
        self.derived = []

    def write_frame(self, frame, received_wall_ms):
        pass

    def write_sensor(self, message, received_wall_ms):
        self.sensors.append((message, received_wall_ms))

    def write_derived(self, record, received_wall_ms):
        self.derived.append((record, received_wall_ms))


@pytest.fixture
def clock() -> FakeTime:
    return FakeTime()


@pytest.fixture
def recorder() -> SpyRecorder:
    return SpyRecorder()


@pytest.fixture
def hub(tmp_path: Path, recorder, clock) -> Hub:
    hub = Hub(Settings(record_dir=tmp_path, web_dist=tmp_path / "missing"), recorder, clock)
    hub.attach_phone()
    return hub


def _sync(hub: Hub, clock: FakeTime) -> None:
    hub.handle_message(
        Ping.model_validate(ping_dict(t=clock.wall_ms(), rtt_ms=20.0, offset_ms=0.0))
    )


def _hello(hub: Hub, **overrides) -> None:
    hub.handle_message(Hello.model_validate(hello_dict(**overrides)))


def _imu(t: float, rotation=(0.0, 0.0, 0.0), count: int = 6) -> Imu:
    """A portrait phone at rest (reads y = -g), with the given rotation columns."""
    samples = [[i * SAMPLE_MS, 0.0, -G, 0.0, *rotation] for i in range(count)]
    return Imu.model_validate({"v": 1, "type": "imu", "t": t, "samples": samples})


def test_no_motion_before_any_imu(hub):
    assert hub.snapshot.motion is None


def test_imu_batch_updates_the_motion_reading(hub, clock):
    _hello(hub, imu_axes="device_xyz")
    _sync(hub, clock)
    batch = _imu(clock.wall_ms() - 100.0, rotation=(0.0, 30.0, 0.0))
    hub.handle_message(batch)
    motion = hub.snapshot.motion
    assert motion.value.yaw_rate_dps == pytest.approx(30.0)
    # Aged by the newest sample's phone time.
    newest_ms = batch.t + batch.samples[-1][0]
    expected_age_s = (clock.wall_ms() - newest_ms + 10.0) / 1000.0
    assert motion.captured_mono == pytest.approx(clock.mono() - expected_age_s)
    assert is_fresh(motion, MOTION_STALE_MS, clock.mono())


def test_motion_is_never_fresh_before_clock_sync(hub, clock):
    _hello(hub, imu_axes="device_xyz")
    hub.handle_message(_imu(clock.wall_ms()))
    assert hub.snapshot.motion.captured_mono is None
    assert not is_fresh(hub.snapshot.motion, MOTION_STALE_MS, clock.mono())


def test_old_order_imu_is_remapped_without_the_marker(hub, clock):
    _hello(hub)  # an older page: rx, ry, rz hold [about y, about z, about x]
    hub.handle_message(_imu(clock.wall_ms(), rotation=(30.0, 0.0, 0.0)))
    assert hub.snapshot.motion.value.yaw_rate_dps == pytest.approx(30.0)


def test_device_xyz_imu_is_used_as_is(hub, clock):
    _hello(hub, imu_axes="device_xyz")
    hub.handle_message(_imu(clock.wall_ms(), rotation=(30.0, 0.0, 0.0)))
    # Rotation about device x is a pitch for a portrait phone, not a turn.
    assert hub.snapshot.motion.value.yaw_rate_dps == pytest.approx(0.0)


def test_each_imu_batch_offers_a_motion_line(hub, clock, recorder):
    _hello(hub, imu_axes="device_xyz")
    batch = _imu(clock.wall_ms(), count=3)
    hub.handle_message(batch)
    assert [message.type for message, _ in recorder.sensors] == ["hello", "imu"]
    ((record, wall_ms),) = recorder.derived
    assert wall_ms == clock.wall_ms()
    assert record["type"] == "motion"
    assert record["v"] == 1
    assert record["t"] == batch.t
    assert [row[0] for row in record["samples"]] == [0.0, SAMPLE_MS, 2 * SAMPLE_MS]
    assert "speed_source" in record


def test_rate_limited_imu_gets_no_motion_line(tmp_path, recorder, clock):
    settings = Settings(record_dir=tmp_path, web_dist=tmp_path / "missing", max_text_rate_hz=1)
    hub = Hub(settings, recorder, clock)
    hub.attach_phone()
    hub.handle_message(_imu(clock.wall_ms()))
    hub.handle_message(_imu(clock.wall_ms() + 100.0))
    assert len(recorder.sensors) == 1
    assert len(recorder.derived) == 1
    # The live estimate still saw both batches.
    assert hub.snapshot.motion is not None


def test_batch_of_only_bad_samples_leaves_the_reading(hub, clock):
    hub.handle_message(_imu(clock.wall_ms()))
    before = hub.snapshot.motion
    bad = Imu.model_validate(
        {"v": 1, "type": "imu", "t": clock.wall_ms() + 50.0, "samples": [[0, 0, 0, 0, 0, 0, 0]]}
    )
    hub.handle_message(bad)
    assert hub.snapshot.motion is before


def test_gps_speed_reaches_the_motion_reading(hub, clock):
    hub.handle_message(_imu(clock.wall_ms()))
    stamp = hub.snapshot.motion
    hub.handle_message(Gps.model_validate(gps_dict(t=clock.wall_ms(), fix_t=clock.wall_ms())))
    motion = hub.snapshot.motion
    assert motion.value.speed_mps == pytest.approx(1.2)
    assert motion.value.speed_source == "gps"
    # Still aged by the newest IMU sample.
    assert motion.received_mono == stamp.received_mono
    assert motion.captured_mono == stamp.captured_mono


def test_gps_before_any_imu_shows_up_with_the_first_batch(hub, clock):
    hub.handle_message(Gps.model_validate(gps_dict(t=clock.wall_ms(), fix_t=clock.wall_ms())))
    assert hub.snapshot.motion is None
    hub.handle_message(_imu(clock.wall_ms()))
    assert hub.snapshot.motion.value.speed_mps == pytest.approx(1.2)


def test_reconnect_resets_motion(hub, clock):
    hub.handle_message(Gps.model_validate(gps_dict(t=clock.wall_ms(), fix_t=clock.wall_ms())))
    hub.handle_message(_imu(clock.wall_ms()))
    hub.attach_phone()
    assert hub.snapshot.motion is None
    hub.handle_message(_imu(clock.wall_ms() + 20.0))
    assert hub.snapshot.motion.value.speed_mps is None
