import json

import pytest

from chair_link.protocol import (
    DEFAULT_TTL_MS,
    DriveCommand,
    McuLimits,
    McuStatus,
    ProtocolError,
    build_chair_message,
    build_hello,
    parse_server_message,
)


def drive(**overrides) -> str:
    base = {
        "type": "drive",
        "seq": 7,
        "t": 1794650000123,
        "v_mps": 0.3,
        "w_radps": -0.1,
        "ttl_ms": 200,
    }
    return json.dumps({**base, **overrides})


def test_parses_drive_command():
    assert parse_server_message(drive()) == DriveCommand(seq=7, v_mps=0.3, w_radps=-0.1, ttl_ms=200)


def test_accepts_bytes_and_integer_speeds():
    cmd = parse_server_message(drive(v_mps=0, w_radps=1).encode())
    assert cmd == DriveCommand(seq=7, v_mps=0.0, w_radps=1.0, ttl_ms=200)
    assert isinstance(cmd.v_mps, float) and isinstance(cmd.w_radps, float)


def test_missing_ttl_uses_default():
    data = json.loads(drive())
    del data["ttl_ms"]
    assert parse_server_message(json.dumps(data)).ttl_ms == DEFAULT_TTL_MS


def test_unknown_message_types_are_ignored():
    assert parse_server_message(json.dumps({"type": "welcome", "x": 1})) is None


@pytest.mark.parametrize(
    "payload, message",
    [
        ("{not json", "JSON"),
        ("[1, 2]", "object"),
        (json.dumps({"seq": 1}), "type"),
        (drive(seq=-1), "seq"),
        (drive(seq=True), "seq"),
        (drive(seq=2**31), "seq"),
        (drive(seq=1.5), "seq"),
        (drive(v_mps=-0.1), "v_mps"),
        (drive(v_mps="fast"), "v_mps"),
        (drive(v_mps=False), "v_mps"),
        (drive(w_radps=None), "w_radps"),
        (drive(ttl_ms=0), "ttl_ms"),
        (drive(ttl_ms=1.5), "ttl_ms"),
        (drive(ttl_ms=10**9), "ttl_ms"),
        ('{"type":"drive","seq":1,"v_mps":NaN,"w_radps":0,"ttl_ms":200}', "v_mps"),
        ('{"type":"drive","seq":1,"v_mps":0,"w_radps":Infinity,"ttl_ms":200}', "w_radps"),
    ],
)
def test_rejects_invalid_drive_commands(payload, message):
    with pytest.raises(ProtocolError, match=message):
        parse_server_message(payload)


def test_hello_reports_firmware_and_compiled_caps():
    hello = build_hello(
        "chair-link 0.1.0", McuLimits(max_speed_mps=0.5, max_turn_radps=0.5, max_ttl_ms=500)
    )
    assert hello == {
        "type": "hello",
        "role": "chair",
        "firmware": "chair-link 0.1.0",
        "max_speed_mps": 0.5,
        "max_turn_radps": 0.5,
    }


def test_limits_from_mcu_array():
    assert McuLimits.from_array([500, 250, 500]) == McuLimits(0.5, 0.25, 500)


@pytest.mark.parametrize("bad", [None, [500, 250], [500, "x", 500], [-1, 250, 500], "abc"])
def test_limits_reject_bad_arrays(bad):
    with pytest.raises(ProtocolError, match="limits"):
        McuLimits.from_array(bad)


def test_status_from_mcu_array_driving():
    status = McuStatus.from_array([1, 0, 0, 48213, 600, -150, 0])
    assert status == McuStatus(
        mode_auto=True,
        joystick_active=False,
        estop=False,
        applied_seq=48213,
        out_v_mps=0.6,
        out_w_radps=-0.15,
        stopped_reason=None,
    )


@pytest.mark.parametrize(
    "code, reason",
    [(0, None), (1, "watchdog"), (2, "estop"), (3, "joystick"), (4, "no_link"), (5, "limit")],
)
def test_status_reason_codes(code, reason):
    assert McuStatus.from_array([1, 0, 0, -1, 0, 0, code]).stopped_reason == reason


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [1, 0, 0, 1, 0, 0],
        [1, 0, 0, 1, 0, 0, 9],
        [1, 0, 0, 1, 0.5, 0, 0],
        [2, 0, 0, 1, 0, 0, 0],
        [1, 0, 0, -2, 0, 0, 0],
        [True, 0, 0, 1, 0, 0, 0],
    ],
)
def test_status_rejects_bad_arrays(bad):
    with pytest.raises(ProtocolError, match="status"):
        McuStatus.from_array(bad)


def test_chair_message_matches_wire_protocol():
    status = McuStatus.from_array([0, 1, 0, -1, 0, 0, 3])
    msg = build_chair_message(status, now_ms=1794650000140)
    assert msg == {
        "type": "chair",
        "t": 1794650000140,
        "mode": "manual",
        "joystick_active": True,
        "estop": False,
        "battery_pct": None,
        "applied_seq": None,
        "output": {"v_mps": 0.0, "w_radps": 0.0},
        "stopped_reason": "joystick",
    }
    assert json.loads(json.dumps(msg)) == msg


def test_chair_message_reports_auto_and_applied_seq():
    msg = build_chair_message(McuStatus.from_array([1, 0, 0, 12, 300, 100, 0]), now_ms=5)
    assert msg["mode"] == "auto" and msg["applied_seq"] == 12
    assert msg["output"] == {"v_mps": 0.3, "w_radps": 0.1}
