from pathlib import Path

import pytest

from drive_service.config import Settings
from drive_service.hub import Hub
from drive_service.protocol import Gps, Heading, Hello, Imu, Ping, Pong
from drive_service.state import age_ms, is_fresh
from helpers import FakeTime, gps_dict, heading_dict, hello_dict, imu_dict, make_frame, ping_dict

CLIENT_MAX_FPS = 60
JITTER_S = 0.005


class SpyRecorder:
    def __init__(self) -> None:
        self.frames = []
        self.sensors = []
        self.derived = []

    def write_frame(self, frame, received_wall_ms):
        self.frames.append((frame, received_wall_ms))

    def write_sensor(self, message, received_wall_ms):
        self.sensors.append((message, received_wall_ms))

    def write_derived(self, record, received_wall_ms):
        self.derived.append((record, received_wall_ms))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(record_dir=tmp_path, web_dist=tmp_path / "missing", max_frame_rate_hz=5)


@pytest.fixture
def clock() -> FakeTime:
    return FakeTime()


@pytest.fixture
def recorder() -> SpyRecorder:
    return SpyRecorder()


@pytest.fixture
def hub(settings, recorder, clock) -> Hub:
    return Hub(settings, recorder, clock)


def _sync(hub: Hub, clock: FakeTime, offset_ms: float = 0.0, rtt_ms: float = 20.0) -> None:
    hub.handle_message(
        Ping.model_validate(ping_dict(t=clock.wall_ms(), rtt_ms=rtt_ms, offset_ms=offset_ms))
    )


def test_starts_empty(hub):
    snapshot = hub.snapshot
    assert snapshot.frame is None
    assert snapshot.gps is None
    assert snapshot.heading is None
    assert snapshot.phone_connected is False


def test_newest_phone_takes_the_slot(hub):
    first = hub.attach_phone()
    second = hub.attach_phone()
    assert first != second
    assert hub.holds_phone(second) is True
    assert hub.holds_phone(first) is False
    assert hub.snapshot.phone_connected is True


def test_old_lease_detach_does_not_disconnect_new_phone(hub):
    first = hub.attach_phone()
    second = hub.attach_phone()
    hub.detach_phone(first)
    assert hub.snapshot.phone_connected is True
    assert hub.holds_phone(second) is True
    hub.detach_phone(second)
    assert hub.snapshot.phone_connected is False
    assert hub.holds_phone(second) is False


def test_attach_forgets_previous_phone(hub):
    lease = hub.attach_phone()
    hub.handle_message(Hello.model_validate(hello_dict()))
    hub.handle_message(Ping.model_validate(ping_dict(rtt_ms=5.0, offset_ms=12345.0)))
    hub.detach_phone(lease)
    hub.attach_phone()
    assert hub.snapshot.hello is None
    assert hub.snapshot.clock is None
    # The old low-RTT sample must not win over the new phone's measurement.
    hub.handle_message(Ping.model_validate(ping_dict(rtt_ms=40.0, offset_ms=0.0)))
    assert hub.snapshot.clock.offset_ms == 0.0


def test_attach_keeps_stats_and_last_readings(hub):
    hub.attach_phone()
    hub.handle_frame(make_frame(seq=3))
    hub.attach_phone()
    assert hub.snapshot.frame.value.header.seq == 3
    assert hub.stats.frames == 1


def test_frame_is_stored_with_receive_time(hub, clock):
    hub.handle_frame(make_frame(seq=3))
    stored = hub.snapshot.frame
    assert stored.value.header.seq == 3
    assert stored.received_mono == clock.mono()


def test_each_update_produces_a_new_snapshot(hub):
    before = hub.snapshot
    hub.handle_frame(make_frame())
    assert before.frame is None
    assert hub.snapshot is not before


def test_readings_go_stale_when_the_stream_stops(hub, clock, settings):
    _sync(hub, clock)
    hub.handle_frame(make_frame(t=clock.wall_ms()))
    assert is_fresh(hub.snapshot.frame, settings.frame_stale_ms, clock.mono())
    clock.advance(1.0)
    assert not is_fresh(hub.snapshot.frame, settings.frame_stale_ms, clock.mono())
    # Aged from capture: one second plus half the 20 ms round trip.
    assert age_ms(hub.snapshot.frame, clock.mono()) == pytest.approx(1010.0)


def test_missing_reading_is_never_fresh(clock):
    assert is_fresh(None, 500.0, clock.mono()) is False
    assert age_ms(None, clock.mono()) is None


def test_frame_burst_keeps_newest(hub, clock, recorder):
    for seq in range(40):
        hub.handle_frame(make_frame(seq=seq))
    assert hub.snapshot.frame.value.header.seq == 39
    assert hub.stats.frames == 40
    clock.advance(0.1)
    hub.handle_frame(make_frame(seq=40))
    assert hub.snapshot.frame.value.header.seq == 40


def test_frame_flood_only_limits_recording(hub, clock, recorder):
    for seq in range(8):
        hub.handle_frame(make_frame(seq=seq))
        clock.advance(0.01)
    assert len(recorder.frames) == 5
    assert hub.rate_limited == 3
    assert hub.snapshot.frame.value.header.seq == 7


def test_frames_at_client_max_rate_are_not_dropped(tmp_path, recorder, clock):
    hub = Hub(Settings(record_dir=tmp_path, web_dist=tmp_path / "missing"), recorder, clock)
    for seq in range(300):
        hub.handle_frame(make_frame(seq=seq))
        jitter = JITTER_S if seq % 2 else -JITTER_S
        clock.advance(1.0 / CLIENT_MAX_FPS + jitter)
    assert hub.rate_limited == 0
    assert len(recorder.frames) == 300


def test_frame_rate_default_has_headroom_over_the_client(tmp_path):
    assert Settings(record_dir=tmp_path).max_frame_rate_hz == 2 * CLIENT_MAX_FPS


def test_gps_heading_and_hello_are_stored(hub):
    hub.handle_message(Gps.model_validate(gps_dict(lat=1.5)))
    hub.handle_message(Heading.model_validate(heading_dict(magnetic_deg=99.0)))
    hub.handle_message(Hello.model_validate(hello_dict()))
    snapshot = hub.snapshot
    assert snapshot.gps.value.lat == 1.5
    assert snapshot.heading.value.magnetic_deg == 99.0
    assert snapshot.hello.camera.label == "Back Ultra Wide Camera"


def test_ping_gets_a_pong_with_server_time(hub, clock):
    reply = hub.handle_message(Ping.model_validate(ping_dict(t=1234.0)))
    assert isinstance(reply, Pong)
    assert reply.t == 1234.0
    assert reply.server_t == clock.wall_ms()


def test_non_ping_messages_get_no_reply(hub):
    assert hub.handle_message(Gps.model_validate(gps_dict())) is None


def test_ping_with_measurement_updates_clock_estimate(hub):
    assert hub.snapshot.clock is None
    hub.handle_message(Ping.model_validate(ping_dict(rtt_ms=30.0, offset_ms=-200.0)))
    assert hub.snapshot.clock.offset_ms == -200.0
    assert hub.snapshot.clock.rtt_ms == 30.0


def test_latency_uses_the_clock_offset(hub, clock):
    # Phone clock runs 200 ms ahead of the server; frame was captured 80 ms ago.
    hub.handle_message(Ping.model_validate(ping_dict(rtt_ms=10.0, offset_ms=-200.0)))
    hub.handle_frame(make_frame(t=clock.wall_ms() + 200.0 - 80.0))
    assert hub.stats.latencies_ms == (pytest.approx(80.0),)


def test_latency_is_unknown_before_clock_sync(hub):
    hub.handle_frame(make_frame())
    assert hub.stats.latencies_ms == ()
    assert hub.stats.frames == 1


def test_everything_is_offered_to_the_recorder(hub, recorder, clock):
    hub.handle_frame(make_frame(seq=1))
    hub.handle_message(Gps.model_validate(gps_dict()))
    hub.handle_message(Imu.model_validate(imu_dict()))
    hub.handle_message(Ping.model_validate(ping_dict()))
    assert [frame.header.seq for frame, _ in recorder.frames] == [1]
    assert [message.type for message, _ in recorder.sensors] == ["gps", "imu"]
    assert recorder.sensors[0][1] == clock.wall_ms()
    assert [record["type"] for record, _ in recorder.derived] == ["motion"]


def test_text_flood_only_limits_recording(tmp_path, recorder, clock):
    settings = Settings(record_dir=tmp_path, web_dist=tmp_path / "missing", max_text_rate_hz=2)
    hub = Hub(settings, recorder, clock)
    for lat in range(5):
        hub.handle_message(Gps.model_validate(gps_dict(lat=float(lat))))
    assert len(recorder.sensors) == 2
    assert hub.rate_limited == 3
    assert hub.snapshot.gps.value.lat == 4.0


def test_pings_are_answered_during_a_text_flood(tmp_path, recorder, clock):
    settings = Settings(record_dir=tmp_path, web_dist=tmp_path / "missing", max_text_rate_hz=1)
    hub = Hub(settings, recorder, clock)
    hub.handle_message(Gps.model_validate(gps_dict()))
    assert isinstance(hub.handle_message(Ping.model_validate(ping_dict())), Pong)


def test_rejected_messages_are_counted(hub):
    hub.note_rejected()
    hub.note_rejected()
    assert hub.rejected == 2
