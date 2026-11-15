import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from drive_service.app import create_app
from drive_service.config import Settings
from helpers import as_json, frame_bytes, gps_dict, hello_dict, ping_dict

HOST = "testserver"
SAME_ORIGIN = {"origin": f"http://{HOST}"}
OTHER_ORIGIN = {"origin": "https://evil.example"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        record_dir=tmp_path / "recordings",
        web_dist=tmp_path / "missing",
        allowed_hosts=(HOST,),
    )


@pytest.fixture
def client(settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _status(client: TestClient) -> dict:
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["error"] is None
    return body["data"]


def test_phone_stream_reaches_status(client):
    with client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone:
        phone.send_text(as_json(hello_dict()))
        phone.send_text(as_json(gps_dict(lat=38.5)))
        phone.send_bytes(frame_bytes(seq=4))
        phone.send_text(as_json(ping_dict(t=77.0)))
        ack = json.loads(phone.receive_text())
        pong = json.loads(phone.receive_text())

        assert ack["type"] == "ack"
        assert ack["seq"] == 4

        assert pong["type"] == "pong"
        assert pong["t"] == 77.0
        status = _status(client)
        assert status["phone_connected"] is True
        assert status["gps"]["lat"] == 38.5
        assert status["frame"]["seq"] == 4
        assert status["camera"]["label"] == "Back Ultra Wide Camera"

    assert _status(client)["phone_connected"] is False


def _pong(phone: WebSocketTestSession) -> None:
    """Round-trip a ping so everything sent before it has been processed."""
    phone.send_text(as_json(ping_dict()))
    assert json.loads(phone.receive_text())["type"] == "pong"


def test_newer_phone_replaces_older(client):
    with ExitStack() as first_stack:
        first = first_stack.enter_context(client.websocket_connect("/ws/phone"))
        _pong(first)
        with client.websocket_connect("/ws/phone") as second:
            with pytest.raises(WebSocketDisconnect) as closed:
                first.receive_text()
            assert closed.value.code == 4410
            first_stack.close()

            second.send_text(as_json(gps_dict(lat=12.5)))
            _pong(second)
            status = _status(client)
            assert status["phone_connected"] is True
            assert status["gps"]["lat"] == 12.5
        assert _status(client)["phone_connected"] is False


def test_replaced_phone_messages_are_ignored(client):
    with client.websocket_connect("/ws/phone") as first:
        _pong(first)
        with client.websocket_connect("/ws/phone") as second:
            _pong(second)
            first.send_text(as_json(gps_dict(lat=-1.0)))
            with pytest.raises(WebSocketDisconnect):
                first.receive_text()
            _pong(second)
            assert _status(client)["gps"] is None


def test_cross_origin_phone_socket_is_refused(client):
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/ws/phone", headers=OTHER_ORIGIN),
    ):
        pass
    assert closed.value.code == 1008


@contextmanager
def _phone_said_hello(client: TestClient) -> Iterator[WebSocketTestSession]:
    """A connected phone whose hello the service has already processed."""
    with client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone:
        phone.send_text(as_json(hello_dict()))
        phone.send_text(as_json(ping_dict()))
        phone.receive_text()  # the hello has been processed once the pong arrives
        yield phone


def test_cross_origin_post_is_forbidden(client):
    with _phone_said_hello(client):
        response = client.post("/api/recording/start", headers=OTHER_ORIGIN)
        assert response.status_code == 403
        assert response.json()["success"] is False
        assert _status(client)["recording"]["active"] is False


ROUTE = [
    {"lat": 40.3487, "lon": -74.6593},
    {"lat": 40.3497, "lon": -74.6593},
    {"lat": 40.3497, "lon": -74.6583},
]


def test_route_goal_round_trip(client):
    assert _status(client)["goal"] is None
    assert client.get("/api/goal").json()["data"] is None

    response = client.post(
        "/api/goal", json={"kind": "route", "points": ROUTE}, headers=SAME_ORIGIN
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "kind": "route",
        "version": 1,
        "instruction": None,
        "route_points": 3,
        "route": ROUTE,
    }
    status = _status(client)
    # The status carries the summary only; the route itself is fetched once.
    assert status["goal"] == {"kind": "route", "version": 1, "instruction": None, "route_points": 3}
    # A route's destination is its last point.
    assert (status["destination"]["lat"], status["destination"]["lon"]) == (40.3497, -74.6583)
    assert client.get("/api/goal").json()["data"]["route"] == ROUTE
