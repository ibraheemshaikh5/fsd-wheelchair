"""The hub with frames from an external camera (/ws/camera) and sensors from the phone or iPad."""

import pytest

from drive_service.config import FRAME_STALE_MS
from drive_service.hub import Hub
from drive_service.protocol import Ping
from drive_service.state import is_fresh
from helpers import FakeTime, NullRecorder, make_frame, ping_dict


@pytest.fixture
def clock() -> FakeTime:
    return FakeTime()


@pytest.fixture
def hub(clock) -> Hub:
    return Hub(NullRecorder(), clock, external_camera=True)


def _ping(clock: FakeTime, offset_ms: float = 0.0, rtt_ms: float = 20.0) -> Ping:
    return Ping.model_validate(ping_dict(t=clock.wall_ms(), rtt_ms=rtt_ms, offset_ms=offset_ms))


def _sync_camera(hub: Hub, clock: FakeTime, offset_ms: float = 0.0) -> None:
    hub.handle_camera_ping(_ping(clock, offset_ms=offset_ms))


def test_external_mode_acks_phone_frames_but_does_not_store_them(hub):
    hub.attach_phone()
    ack = hub.handle_frame(make_frame(seq=7))
    assert ack.seq == 7
    assert hub.snapshot.frame is None


def test_camera_frame_is_aged_by_the_camera_clock(hub, clock):
    hub.attach_camera()
    # The camera's clock runs 5 s behind the service's.
    _sync_camera(hub, clock, offset_ms=5000.0)
    hub.handle_camera_frame(make_frame(t=clock.wall_ms() - 5000.0 - 100.0))
    frame = hub.snapshot.frame
    # 100 ms old plus half the 20 ms round trip.
    assert frame.captured_mono == pytest.approx(clock.mono() - 0.110)
    assert is_fresh(frame, FRAME_STALE_MS, clock.mono())
    clock.advance(0.5)
    assert not is_fresh(hub.snapshot.frame, FRAME_STALE_MS, clock.mono())
