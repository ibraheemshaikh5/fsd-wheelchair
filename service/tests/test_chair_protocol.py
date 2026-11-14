"""Chair wire messages: hello and status in, drive out."""

import json

import pytest
from pydantic import ValidationError

from drive_service.chair.protocol import (
    ChairHello,
    ChairProtocolError,
    ChairStatus,
    DriveCommand,
    parse_chair_message,
)
from test_chair_support import as_json, hello_dict, status_dict

MAX_BYTES = 2048


def test_parses_hello():
    hello = parse_chair_message(as_json(hello_dict()), MAX_BYTES)
    assert isinstance(hello, ChairHello)
    assert hello.firmware == "test-1.0"
    assert hello.max_speed_mps == 0.5


def test_parses_status():
    status = parse_chair_message(as_json(status_dict()), MAX_BYTES)
    assert isinstance(status, ChairStatus)
    assert status.mode == "auto"
    assert status.applied_seq == 3
    assert status.output.v_mps == 0.0


def test_parses_status_with_nulls_and_a_reason():
    status = parse_chair_message(
        as_json(status_dict(battery_pct=None, applied_seq=None, stopped_reason="watchdog")),
        MAX_BYTES,
    )
    assert isinstance(status, ChairStatus)
    assert status.battery_pct is None
    assert status.applied_seq is None
    assert status.stopped_reason == "watchdog"


@pytest.mark.parametrize("reason", ["watchdog", "estop", "joystick", "no_link", "limit"])
def test_every_stop_reason_is_accepted(reason):
    status = parse_chair_message(as_json(status_dict(stopped_reason=reason)), MAX_BYTES)
    assert isinstance(status, ChairStatus)
    assert status.stopped_reason == reason


@pytest.mark.parametrize(
    "text",
    [
        as_json(status_dict(type="drive")),
        as_json(status_dict(extra=1)),
        as_json({k: v for k, v in status_dict().items() if k != "estop"}),
        as_json(status_dict(mode="sport")),
        as_json(status_dict(estop="false")),
        as_json(status_dict(joystick_active=0)),
        as_json(status_dict(battery_pct=150)),
        as_json(status_dict(applied_seq=-1)),
        as_json(status_dict(stopped_reason="tired")),
        as_json(status_dict(output={"v_mps": -0.1, "w_radps": 0.0})),
        as_json(status_dict(output={"v_mps": 0.1})),
        as_json(hello_dict(role="phone")),
        as_json(hello_dict(firmware="x" * 200)),
        as_json(hello_dict(max_speed_mps=-1)),
        as_json(hello_dict(max_speed_mps=100)),
        '{"type": "chair", "t": NaN}',
        json.dumps(hello_dict()).replace("0.5", "Infinity", 1),
        "not json",
        "[1, 2]",
        "",
    ],
)
def test_rejects_bad_messages(text):
    with pytest.raises(ChairProtocolError):
        parse_chair_message(text, MAX_BYTES)


def test_rejects_oversized_message_before_parsing():
    text = as_json(hello_dict(firmware="x" * 50))
    with pytest.raises(ChairProtocolError, match="bytes"):
        parse_chair_message(text, 40)


def test_error_names_the_bad_field():
    with pytest.raises(ChairProtocolError, match="mode"):
        parse_chair_message(as_json(status_dict(mode="sport")), MAX_BYTES)


def test_drive_command_wire_format():
    command = DriveCommand(seq=7, t=1_794_650_000_123, v_mps=0.25, w_radps=-0.1, ttl_ms=200)
    assert json.loads(command.to_wire()) == {
        "type": "drive",
        "seq": 7,
        "t": 1_794_650_000_123,
        "v_mps": 0.25,
        "w_radps": -0.1,
        "ttl_ms": 200,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"v_mps": -0.01},
        {"ttl_ms": 501},
        {"ttl_ms": 0},
        {"seq": -1},
        {"w_radps": float("nan")},
        {"v_mps": float("inf")},
    ],
)
def test_drive_command_refuses_unsafe_values(overrides):
    base = {"seq": 1, "t": 0, "v_mps": 0.0, "w_radps": 0.0, "ttl_ms": 200}
    with pytest.raises(ValidationError):
        DriveCommand(**{**base, **overrides})


def test_messages_are_frozen():
    command = DriveCommand(seq=1, t=0, v_mps=0.0, w_radps=0.0, ttl_ms=200)
    with pytest.raises(ValidationError):
        command.v_mps = 1.0
