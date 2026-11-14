"""The chair HTTP API: link status and bench test commands."""

import json

import pytest
from fastapi.testclient import TestClient

from helpers import FakeTime
from test_chair_support import (
    AUTH,
    OTHER_ORIGIN,
    SAME_ORIGIN,
    as_json,
    chair_settings,
    hello_dict,
    make_app,
    status_dict,
)

GOOD_TEST = {"v_mps": 0.2, "w_radps": -0.1, "duration_ms": 1000}


@pytest.fixture
def clock():
    return FakeTime()


@pytest.fixture
def client(clock):
    with TestClient(make_app(time_source=clock)) as test_client:
        yield test_client


def _drive(ws) -> dict:
    return json.loads(ws.receive_text())


def _settle(chair, count: int = 2) -> dict:
    """Drain a few ticks so the socket has read what was sent; return the last command."""
    message = {}
    for _ in range(count):
        message = _drive(chair)
    return message


def _connect_ready(client, **hello):
    """A chair that has said hello and reported a fresh, all-clear status."""
    chair = client.websocket_connect("/ws/chair", headers=AUTH)
    session = chair.__enter__()
    session.send_text(as_json(hello_dict(**hello)))
    session.send_text(as_json(status_dict(applied_seq=None)))
    _settle(session)
    return chair, session


def test_get_reports_a_disconnected_chair(client):
    response = client.get("/api/chair")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"]["connected"] is False


def test_test_command_needs_a_connected_chair(client):
    response = client.post("/api/chair/test", json=GOOD_TEST)
    assert response.status_code == 409
    assert response.json() == {"success": False, "data": None, "error": response.json()["error"]}
    assert "not connected" in response.json()["error"]


def test_test_command_needs_a_hello(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        _settle(chair)
        response = client.post("/api/chair/test", json=GOOD_TEST)
        assert response.status_code == 409


def test_test_command_needs_a_status_after_hello(client):
    # Until the chair reports, its e-stop and joystick state are unknown.
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        chair.send_text(as_json(hello_dict()))
        _settle(chair)
        response = client.post("/api/chair/test", json=GOOD_TEST)
        assert response.status_code == 409
        assert "not reported" in response.json()["error"]


def test_test_command_needs_a_fresh_status(client, clock):
    chair_cm, _ = _connect_ready(client)
    try:
        clock.advance(0.6)  # older than status_stale_ms (500)
        response = client.post("/api/chair/test", json=GOOD_TEST)
        assert response.status_code == 409
        assert "old" in response.json()["error"]
        assert client.get("/api/chair").json()["data"]["test"]["active"] is False
    finally:
        chair_cm.__exit__(None, None, None)


def test_a_running_bench_test_stops_when_the_status_goes_stale(client, clock):
    chair_cm, chair = _connect_ready(client)
    try:
        long_test = {**GOOD_TEST, "duration_ms": 3000}
        assert client.post("/api/chair/test", json=long_test).status_code == 200
        assert _settle(chair)["v_mps"] == 0.2
        clock.advance(0.6)  # bench still has 2.4 s to run, but the chair went quiet
        assert _settle(chair)["v_mps"] == 0.0
        assert client.get("/api/chair").json()["data"]["test"]["active"] is False
    finally:
        chair_cm.__exit__(None, None, None)


def test_foreign_origin_cannot_start_or_stop(client):
    assert client.post("/api/chair/test", json=GOOD_TEST, headers=OTHER_ORIGIN).status_code == 403
    assert client.delete("/api/chair/test", headers=OTHER_ORIGIN).status_code == 403


def test_configured_origin_may_start():
    app = make_app(allowed_origins=("https://chair.example.ts.net",), time_source=FakeTime())
    with TestClient(app) as client, client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        chair.send_text(as_json(hello_dict()))
        chair.send_text(as_json(status_dict(applied_seq=None)))
        _settle(chair)
        headers = {"origin": "https://chair.example.ts.net"}
        assert client.post("/api/chair/test", json=GOOD_TEST, headers=headers).status_code == 200


def test_bench_command_drives_then_expires(client, clock):
    chair_cm, chair = _connect_ready(client)
    try:
        response = client.post("/api/chair/test", json=GOOD_TEST, headers=SAME_ORIGIN)
        assert response.status_code == 200
        assert response.json()["data"]["test"]["active"] is True
        command = _settle(chair)
        assert (command["v_mps"], command["w_radps"]) == (0.2, -0.1)
        clock.advance(1.0)
        command = _settle(chair)
        assert (command["v_mps"], command["w_radps"]) == (0.0, 0.0)
        assert client.get("/api/chair").json()["data"]["test"]["active"] is False
    finally:
        chair_cm.__exit__(None, None, None)


def test_delete_stops_now(client):
    chair_cm, chair = _connect_ready(client)
    try:
        client.post("/api/chair/test", json=GOOD_TEST, headers=SAME_ORIGIN)
        assert _settle(chair)["v_mps"] == 0.2
        response = client.delete("/api/chair/test", headers=SAME_ORIGIN)
        assert response.status_code == 200
        assert response.json()["data"]["test"]["active"] is False
        assert _drive(chair)["v_mps"] == 0.0
    finally:
        chair_cm.__exit__(None, None, None)


def test_delete_without_a_chair_is_fine(client):
    response = client.delete("/api/chair/test")
    assert response.status_code == 200
    assert response.json()["success"] is True


@pytest.mark.parametrize(
    "body",
    [
        {**GOOD_TEST, "v_mps": 0.51},
        {**GOOD_TEST, "v_mps": -0.1},
        {**GOOD_TEST, "w_radps": 0.6},
        {**GOOD_TEST, "w_radps": -0.6},
        {**GOOD_TEST, "duration_ms": 3001},
        {**GOOD_TEST, "duration_ms": 0},
        {**GOOD_TEST, "duration_ms": 1.5},
        {**GOOD_TEST, "v_mps": "0.2"},
        {**GOOD_TEST, "extra": 1},
        {"v_mps": 0.2, "w_radps": 0.0},
    ],
)
def test_out_of_range_test_commands_are_refused(client, body):
    chair_cm, _ = _connect_ready(client)
    try:
        response = client.post("/api/chair/test", json=body, headers=SAME_ORIGIN)
        assert response.status_code == 422
        assert response.json()["success"] is False
        assert client.get("/api/chair").json()["data"]["test"]["active"] is False
    finally:
        chair_cm.__exit__(None, None, None)


def test_board_caps_lower_than_the_service_caps_are_respected(client):
    chair_cm, _ = _connect_ready(client, max_speed_mps=0.3)
    try:
        body = {**GOOD_TEST, "v_mps": 0.4}
        response = client.post("/api/chair/test", json=body, headers=SAME_ORIGIN)
        assert response.status_code == 422
        assert "0.3" in response.json()["error"]
    finally:
        chair_cm.__exit__(None, None, None)


@pytest.mark.parametrize(
    "content", [b"not json", b'{"v_mps": NaN, "w_radps": 0, "duration_ms": 10}', b"[1]"]
)
def test_bad_json_is_refused(client, content):
    chair_cm, _ = _connect_ready(client)
    try:
        response = client.post("/api/chair/test", content=content, headers=SAME_ORIGIN)
        assert response.status_code in (400, 422)
        assert response.json()["success"] is False
    finally:
        chair_cm.__exit__(None, None, None)


def test_oversized_body_is_refused(client):
    response = client.post("/api/chair/test", content=b" " * 5000, headers=SAME_ORIGIN)
    assert response.status_code == 413


def _chunks(total: int, size: int = 256):
    for _ in range(total // size):
        yield b" " * size


def test_oversized_chunked_body_is_refused_without_reading_it_all(client):
    # No content-length: the body arrives in chunks and is cut off once over the limit.
    response = client.post("/api/chair/test", content=_chunks(1_000_000))
    assert response.status_code == 413


def test_declared_oversized_body_is_refused_up_front(client):
    response = client.post("/api/chair/test", content=b"{}", headers={"content-length": "10000000"})
    assert response.status_code == 413


def test_unreadable_content_length_is_refused(client):
    response = client.post("/api/chair/test", content=b"{}", headers={"content-length": "lots"})
    assert response.status_code == 400


def test_small_chunked_body_is_read(client):
    chair_cm, _ = _connect_ready(client)
    try:
        body = json.dumps(GOOD_TEST).encode()
        response = client.post("/api/chair/test", content=iter([body[:10], body[10:]]))
        assert response.status_code == 200, response.text
    finally:
        chair_cm.__exit__(None, None, None)


@pytest.mark.parametrize(
    "status", [status_dict(estop=True), status_dict(mode="manual", joystick_active=True)]
)
def test_refused_while_the_chair_reports_estop_or_manual(client, status):
    chair_cm, chair = _connect_ready(client)
    try:
        chair.send_text(as_json(status))
        _settle(chair)
        response = client.post("/api/chair/test", json=GOOD_TEST, headers=SAME_ORIGIN)
        assert response.status_code == 409
    finally:
        chair_cm.__exit__(None, None, None)


def test_disconnect_cancels_the_bench_command(client):
    chair_cm, _ = _connect_ready(client)
    client.post("/api/chair/test", json=GOOD_TEST, headers=SAME_ORIGIN)
    chair_cm.__exit__(None, None, None)
    data = client.get("/api/chair").json()["data"]
    assert data["connected"] is False
    assert data["test"]["active"] is False


def test_status_reported_by_the_chair_shows_its_age(client, clock):
    chair_cm, chair = _connect_ready(client)
    try:
        chair.send_text(as_json(status_dict()))
        _settle(chair)
        clock.advance(0.6)
        data = client.get("/api/chair").json()["data"]
        assert data["status_age_ms"] == 600
        assert data["status_fresh"] is False
    finally:
        chair_cm.__exit__(None, None, None)


def test_no_token_still_serves_status():
    with TestClient(make_app(chair_settings(token=None))) as client:
        assert client.get("/api/chair").json()["data"]["connected"] is False
