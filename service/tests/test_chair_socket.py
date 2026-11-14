"""The /ws/chair socket: auth, origin, cadence, newest-wins and bad messages."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from drive_service.chair.sockets import (
    CLOSE_CHAIR_IDLE,
    CLOSE_CHAIR_REPLACED,
    CLOSE_POLICY_VIOLATION,
    bearer_token_ok,
)
from helpers import FakeTime
from test_chair_support import (
    AUTH,
    OTHER_ORIGIN,
    SAME_ORIGIN,
    TOKEN,
    as_json,
    chair_settings,
    hello_dict,
    make_app,
    status_dict,
)


@pytest.fixture
def client():
    with TestClient(make_app(time_source=FakeTime())) as test_client:
        yield test_client


def _drive(ws) -> dict:
    message = json.loads(ws.receive_text())
    assert message["type"] == "drive"
    return message


def _link(client) -> dict:
    return client.get("/api/chair").json()["data"]


def _refused(client, headers) -> int:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/ws/chair", headers=headers),
    ):
        pass  # pragma: no cover - the handshake is refused
    return closed.value.code


def test_no_token_configured_refuses_every_chair():
    app = make_app(chair_settings(token=None))
    with TestClient(app) as client:
        assert _refused(client, AUTH) == CLOSE_POLICY_VIOLATION
        assert _refused(client, {}) == CLOSE_POLICY_VIOLATION


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong-token-0123456789"},
        {"authorization": f"Basic {TOKEN}"},
        {"authorization": f"Bearer {TOKEN}x"},
        {"authorization": "Bearer "},
    ],
)
def test_wrong_or_missing_token_is_refused(client, headers):
    assert _refused(client, headers) == CLOSE_POLICY_VIOLATION
    assert _link(client)["connected"] is False


@pytest.mark.parametrize("origin", [OTHER_ORIGIN, SAME_ORIGIN])
def test_any_browser_origin_is_refused_even_with_the_right_token(client, origin):
    assert _refused(client, {**AUTH, **origin}) == CLOSE_POLICY_VIOLATION


def test_bearer_check_without_a_configured_token_is_false():
    assert bearer_token_ok({"authorization": f"Bearer {TOKEN}"}, None) is False


def test_right_token_connects_and_gets_stop_commands(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        first = _drive(chair)
        second = _drive(chair)
        assert (first["v_mps"], first["w_radps"]) == (0.0, 0.0)
        assert first["ttl_ms"] == 200
        assert second["seq"] == first["seq"] + 1
        assert isinstance(first["t"], int)
        assert _link(client)["connected"] is True
    assert _link(client)["connected"] is False


def test_commands_come_at_about_20_hz(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        _drive(chair)
        started = time.monotonic()
        for _ in range(10):
            _drive(chair)
        elapsed = time.monotonic() - started
    # Ten intervals of 50 ms.
    assert 0.35 < elapsed < 0.9


def test_hello_and_status_show_up_in_the_link(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        chair.send_text(as_json(hello_dict(firmware="bench-2")))
        chair.send_text(as_json(status_dict(battery_pct=55)))
        _drive(chair)
        _drive(chair)
        link = _link(client)
        assert link["hello"]["firmware"] == "bench-2"
        assert link["status"]["battery_pct"] == 55
        assert link["status_fresh"] is True
        assert link["last_command"]["type"] == "drive"


def _send_bad(chair, count: int) -> None:
    for _ in range(count):
        chair.send_text("garbage")


def test_repeated_bad_messages_close_the_socket(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        _send_bad(chair, 10)
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                _drive(chair)
        assert closed.value.code == CLOSE_POLICY_VIOLATION


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "websocket.receive", "bytes": b"\x00\x01"},
        {"type": "websocket.receive", "text": as_json(status_dict())},  # status before hello
    ],
)
def test_binary_and_status_before_hello_count_as_bad(client, bad):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        for _ in range(10):
            chair.send(bad)
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                _drive(chair)
        assert closed.value.code == CLOSE_POLICY_VIOLATION


def test_a_second_hello_is_rejected_and_counted(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        chair.send_text(as_json(hello_dict()))
        chair.send_text(as_json(hello_dict(firmware="other")))
        _drive(chair)
        _drive(chair)
        link = _link(client)
        assert link["hello"]["firmware"] == "test-1.0"
        assert link["rejected"] == 1


def test_a_good_message_resets_the_error_streak(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        chair.send_text(as_json(hello_dict()))
        for _ in range(3):
            _send_bad(chair, 9)
            chair.send_text(as_json(status_dict()))
        for _ in range(3):
            _drive(chair)
        assert _link(client)["connected"] is True
        assert _link(client)["rejected"] == 27


def test_newest_connection_wins(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as first:
        first.send_text(as_json(hello_dict(firmware="old")))
        _drive(first)
        with client.websocket_connect("/ws/chair", headers=AUTH) as second:
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    _drive(first)
            assert closed.value.code == CLOSE_CHAIR_REPLACED
            second.send_text(as_json(hello_dict(firmware="new")))
            _drive(second)
            _drive(second)
            link = _link(client)
            assert link["connected"] is True
            assert link["hello"]["firmware"] == "new"
    assert _link(client)["connected"] is False


def test_seq_keeps_counting_across_reconnects(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        last = _drive(chair)["seq"]
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        assert _drive(chair)["seq"] > last


def test_silent_chair_is_dropped():
    settings = chair_settings(idle_timeout_ms=300)
    with (
        TestClient(make_app(settings, FakeTime())) as client,
        client.websocket_connect("/ws/chair", headers=AUTH) as chair,
    ):
        started = time.monotonic()
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                _drive(chair)
        assert closed.value.code == CLOSE_CHAIR_IDLE
        assert time.monotonic() - started < 1.0
