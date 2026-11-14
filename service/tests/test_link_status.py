"""The hub keeps the latest link stats by receive time; the status reports them."""

import json
from pathlib import Path

import pytest

from drive_service.config import Settings
from drive_service.hub import Hub
from drive_service.protocol import Ack, Link
from drive_service.recorder import RecordingInfo
from drive_service.state import SensorSnapshot, Stamped
from drive_service.stats import StreamStats
from drive_service.status import build_status
from helpers import FakeTime, link_dict, make_frame

LINK_STALE_S = 3.0


class SpyRecorder:
    def __init__(self) -> None:
        self.sensors = []

    def write_frame(self, frame, received_wall_ms):
        pass

    def write_sensor(self, message, received_wall_ms):
        self.sensors.append((message, received_wall_ms))

    def write_derived(self, record, received_wall_ms):
        pass


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(record_dir=tmp_path, web_dist=tmp_path / "missing")


@pytest.fixture
def clock() -> FakeTime:
    return FakeTime()


@pytest.fixture
def recorder() -> SpyRecorder:
    return SpyRecorder()


@pytest.fixture
def hub(settings, recorder, clock) -> Hub:
    return Hub(settings, recorder, clock)


def _link(**overrides) -> Link:
    return Link.model_validate(link_dict(**overrides))


def _status(snapshot: SensorSnapshot, now_mono: float, settings: Settings) -> dict:
    return build_status(snapshot, StreamStats(), RecordingInfo(), 0, 0, now_mono, settings)


def test_link_stale_default_is_three_seconds(tmp_path):
    assert Settings(record_dir=tmp_path).link_stale_ms == 3000.0


def test_frame_returns_an_ack_stamped_with_server_wall_time(hub, clock):
    ack = hub.handle_frame(make_frame(seq=21))
    assert ack == Ack(seq=21, t=clock.wall_ms())


def test_ack_for_a_refused_frame_uses_server_wall_time(hub, clock):
    clock.advance(0.25)
    assert hub.ack_frame(8) == Ack(seq=8, t=clock.wall_ms())


def test_link_is_stored_by_receive_time_and_gets_no_reply(hub, clock):
    link = _link()
    assert hub.handle_message(link) is None
    stored = hub.snapshot.link
    assert stored.value == link
    assert stored.received_mono == clock.mono()
    # Link stats are judged by arrival only, never by the phone's clock.
    assert stored.captured_mono is None


def test_link_is_offered_to_the_recorder(hub, recorder, clock):
    link = _link()
    hub.handle_message(link)
    assert recorder.sensors == [(link, clock.wall_ms())]


def test_newest_link_replaces_older(hub):
    hub.handle_message(_link(sent=1))
    hub.handle_message(_link(sent=2))
    assert hub.snapshot.link.value.sent == 2


def test_a_new_connection_forgets_the_previous_link_stats(hub):
    hub.attach_phone()
    hub.handle_message(_link())
    hub.attach_phone()
    assert hub.snapshot.link is None


def test_link_stats_survive_a_disconnect_and_age_out(hub, clock, settings):
    lease = hub.attach_phone()
    hub.handle_message(_link())
    hub.detach_phone(lease)
    clock.advance(LINK_STALE_S + 0.1)
    status = _status(hub.snapshot, clock.mono(), settings)
    assert status["link"]["fresh"] is False


def test_status_link_is_null_without_link_stats(settings, clock):
    assert _status(SensorSnapshot(), clock.mono(), settings)["link"] is None


def test_status_link_reports_every_field_but_v_type_and_t(settings, clock):
    snapshot = SensorSnapshot(link=Stamped(_link(), clock.mono()))
    clock.advance(0.5)
    status = _status(snapshot, clock.mono(), settings)
    json.dumps(status)
    assert status["link"] == {
        "captured": 600,
        "sent": 240,
        "acked": 236,
        "skipped_flow": 300,
        "skipped_encoder": 40,
        "skipped_socket": 20,
        "in_flight": 3,
        "jpeg_quality": 0.5,
        "ack_rtt_ms": 180.0,
        "age_ms": pytest.approx(500.0),
        "fresh": True,
    }


@pytest.mark.parametrize(("elapsed_s", "fresh"), [(3.0, True), (3.001, False)])
def test_status_link_goes_stale_after_three_seconds(settings, clock, elapsed_s, fresh):
    snapshot = SensorSnapshot(link=Stamped(_link(), clock.mono()))
    clock.advance(elapsed_s)
    assert _status(snapshot, clock.mono(), settings)["link"]["fresh"] is fresh
