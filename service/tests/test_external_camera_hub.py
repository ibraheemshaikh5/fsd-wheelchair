"""The hub with frames from an external camera (/ws/camera) and sensors from the phone or iPad."""

from pathlib import Path

import pytest

from drive_service.config import Settings
from drive_service.hub import Hub
from drive_service.protocol import Gps, Hello, Ping
from drive_service.recorder import RecordingInfo
from drive_service.state import is_fresh
from drive_service.status import build_status
from helpers import FakeTime, NullRecorder, gps_dict, hello_dict, make_frame, ping_dict


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(record_dir=tmp_path, web_dist=tmp_path / "missing")


@pytest.fixture
def clock() -> FakeTime:
    return FakeTime()


@pytest.fixture
def hub(settings, clock) -> Hub:
    return Hub(settings, NullRecorder(), clock, external_camera=True)


def _ping(clock: FakeTime, offset_ms: float = 0.0, rtt_ms: float = 20.0) -> Ping:
    return Ping.model_validate(ping_dict(t=clock.wall_ms(), rtt_ms=rtt_ms, offset_ms=offset_ms))


def _sync_phone(hub: Hub, clock: FakeTime) -> None:
    hub.handle_message(_ping(clock))


def _sync_camera(hub: Hub, clock: FakeTime, offset_ms: float = 0.0) -> None:
    hub.handle_camera_ping(_ping(clock, offset_ms=offset_ms))


def _status(hub: Hub, clock: FakeTime, settings: Settings) -> dict:
    return build_status(
        hub.snapshot,
        hub.stats,
        RecordingInfo(),
        hub.rejected,
        hub.rate_limited,
        clock.mono(),
        settings,
        external_camera=hub.external_camera,
    )


def test_phone_mode_keeps_storing_phone_frames(settings, clock):
    hub = Hub(settings, NullRecorder(), clock)
    assert hub.external_camera is False
    hub.attach_phone()
    ack = hub.handle_frame(make_frame(seq=3))
    assert ack.seq == 3
    assert hub.snapshot.frame is not None


def test_external_mode_acks_phone_frames_but_does_not_store_them(hub):
    hub.attach_phone()
    ack = hub.handle_frame(make_frame(seq=7))
    assert ack.seq == 7
    assert hub.snapshot.frame is None


def test_camera_frame_is_stored_and_wakes_the_model_feed(hub, clock):
    hub.attach_camera()
    arrived = hub.frame_arrived()
    ack = hub.handle_camera_frame(make_frame(seq=1, t=clock.wall_ms()))
    assert ack.seq == 1
    assert hub.snapshot.frame is not None
    assert arrived.is_set()


def test_camera_frame_before_camera_clock_sync_is_never_fresh(hub, clock, settings):
    hub.attach_camera()
    _sync_phone(hub, clock)  # the phone's clock must not stand in for the camera's
    hub.handle_camera_frame(make_frame(t=clock.wall_ms()))
    assert hub.snapshot.frame.captured_mono is None
    assert not is_fresh(hub.snapshot.frame, settings.frame_stale_ms, clock.mono())


def test_camera_frame_is_aged_by_the_camera_clock(hub, clock, settings):
    hub.attach_camera()
    # The camera's clock runs 5 s behind the service's.
    _sync_camera(hub, clock, offset_ms=5000.0)
    hub.handle_camera_frame(make_frame(t=clock.wall_ms() - 5000.0 - 100.0))
    frame = hub.snapshot.frame
    # 100 ms old plus half the 20 ms round trip.
    assert frame.captured_mono == pytest.approx(clock.mono() - 0.110)
    assert is_fresh(frame, settings.frame_stale_ms, clock.mono())
    clock.advance(0.5)
    assert not is_fresh(hub.snapshot.frame, settings.frame_stale_ms, clock.mono())


def test_camera_frame_from_the_future_drops_the_camera_clock_only(hub, clock):
    hub.attach_phone()
    _sync_phone(hub, clock)
    hub.attach_camera()
    _sync_camera(hub, clock)
    hub.handle_camera_frame(make_frame(t=clock.wall_ms() + 5000.0))
    assert hub.snapshot.camera_clock is None
    assert hub.snapshot.frame.captured_mono is None
    assert hub.snapshot.clock is not None


def test_new_camera_connection_forgets_the_old_camera_clock_and_hello(hub, clock):
    first = hub.attach_camera()
    _sync_camera(hub, clock)
    hub.handle_camera_hello(Hello.model_validate(hello_dict()))
    second = hub.attach_camera()
    assert not hub.holds_camera(first)
    assert hub.holds_camera(second)
    assert hub.snapshot.camera_clock is None
    assert hub.snapshot.camera_hello is None


def test_phone_reconnect_keeps_the_camera_clock(hub, clock):
    hub.attach_camera()
    _sync_camera(hub, clock)
    hub.attach_phone()
    assert hub.snapshot.camera_clock is not None


def test_detach_camera_ignores_a_stale_lease(hub):
    old = hub.attach_camera()
    hub.attach_camera()
    hub.detach_camera(old)
    assert hub.snapshot.camera_connected is True


def test_detach_camera_marks_it_gone(hub):
    lease = hub.attach_camera()
    hub.detach_camera(lease)
    assert hub.snapshot.camera_connected is False


def test_camera_ping_answers_with_service_time(hub, clock):
    pong = hub.handle_camera_ping(_ping(clock))
    assert pong.server_t == clock.wall_ms()


def _all_live(hub: Hub, clock: FakeTime) -> None:
    hub.attach_phone()
    _sync_phone(hub, clock)
    hub.handle_message(Gps.model_validate(gps_dict(t=clock.wall_ms(), fix_t=clock.wall_ms())))
    hub.attach_camera()
    _sync_camera(hub, clock)
    hub.handle_camera_hello(
        Hello.model_validate(
            hello_dict(
                user_agent="camera-sender",
                camera={"label": "USB Live camera", "width": 480, "height": 360, "fps": 20},
            )
        )
    )
    hub.handle_camera_frame(make_frame(t=clock.wall_ms()))


def test_status_sensors_ok_with_external_camera_and_ipad_gps(hub, clock, settings):
    _all_live(hub, clock)
    status = _status(hub, clock, settings)
    assert status["sensors_ok"] is True
    assert status["frame_source"] == {"kind": "external", "connected": True, "clock_synced": True}
    assert status["camera"]["label"] == "USB Live camera"


def test_status_not_ok_once_the_camera_leaves(hub, clock, settings):
    _all_live(hub, clock)
    hub.detach_camera(hub.attach_camera())
    status = _status(hub, clock, settings)
    assert status["sensors_ok"] is False
    assert status["frame_source"]["connected"] is False


def test_status_not_ok_without_the_phone(hub, clock, settings):
    _all_live(hub, clock)
    hub.detach_phone(hub.attach_phone())
    assert _status(hub, clock, settings)["sensors_ok"] is False


def test_status_phone_mode_reports_the_phone_as_frame_source(settings, clock):
    hub = Hub(settings, NullRecorder(), clock)
    hub.attach_phone()
    _sync_phone(hub, clock)
    status = _status(hub, clock, settings)
    assert status["frame_source"] == {"kind": "phone", "connected": True, "clock_synced": True}


def test_hello_without_a_camera_is_valid():
    hello = Hello.model_validate(hello_dict(user_agent="iPad", camera=None))
    assert hello.camera is None
