"""The phone slot: the newest connection holds it, and an old lease can do nothing."""

import pytest

from drive_service.hub import Hub
from drive_service.protocol import Hello, Ping
from helpers import FakeTime, NullRecorder, hello_dict, make_frame, ping_dict


@pytest.fixture
def hub() -> Hub:
    return Hub(NullRecorder(), FakeTime())


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
