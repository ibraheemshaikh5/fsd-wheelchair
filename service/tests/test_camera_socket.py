"""The /ws/camera socket: who may connect, clock sync, frames, and newest-wins."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from starlette.websockets import WebSocketDisconnect

from drive_service.app import create_app
from drive_service.camera import CameraSettings
from drive_service.camera.sockets import CLOSE_CAMERA_REPLACED, CLOSE_POLICY_VIOLATION
from drive_service.config import Settings
from helpers import as_json, frame_bytes, gps_dict, hello_dict, ping_dict

TOKEN = "camera-token-for-tests-0123456789"
AUTH = {"authorization": f"Bearer {TOKEN}"}
TEST_HOSTS = ("testserver", "localhost", "127.0.0.1")


def camera_settings(**overrides: Any) -> CameraSettings:
    """Settings that never read the developer's .env file."""
    base: dict[str, Any] = {"source": "external", "token": SecretStr(TOKEN)}
    return CameraSettings(_env_file=None, **{**base, **overrides})


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        record_dir=tmp_path / "recordings",
        web_dist=tmp_path / "missing",
        allowed_hosts=TEST_HOSTS,
        max_consecutive_errors=3,
    )


@pytest.fixture
def client(tmp_path):
    app = create_app(_settings(tmp_path), camera_settings=camera_settings())
    with TestClient(app) as test_client:
        yield test_client


def _refused(client: TestClient, headers: dict[str, str]) -> int:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/ws/camera", headers=headers),
    ):
        pass  # pragma: no cover - the handshake is refused
    return closed.value.code


def _status(client: TestClient) -> dict:
    return client.get("/api/status").json()["data"]


def test_refused_without_a_configured_token(tmp_path):
    app = create_app(_settings(tmp_path), camera_settings=camera_settings(token=None))
    with TestClient(app) as client:
        assert _refused(client, AUTH) == CLOSE_POLICY_VIOLATION


def test_refused_while_frames_come_from_the_phone(tmp_path):
    app = create_app(_settings(tmp_path), camera_settings=camera_settings(source="phone"))
    with TestClient(app) as client:
        assert _refused(client, AUTH) == CLOSE_POLICY_VIOLATION
        assert _status(client)["frame_source"]["kind"] == "phone"


def test_refused_with_a_wrong_or_missing_token(client):
    assert _refused(client, {}) == CLOSE_POLICY_VIOLATION
    assert _refused(client, {"authorization": "Bearer wrong-token-0123456789"}) == 1008


def test_refused_from_a_browser(client):
    headers = {**AUTH, "origin": "http://testserver"}
    assert _refused(client, headers) == CLOSE_POLICY_VIOLATION


def test_ping_hello_and_frame_reach_the_status(client):
    with client.websocket_connect("/ws/camera", headers=AUTH) as ws:
        ws.send_text(as_json(hello_dict(user_agent="camera-sender")))
        ws.send_text(as_json(ping_dict(rtt_ms=2.0, offset_ms=0.0)))
        pong = json.loads(ws.receive_text())
        assert pong["type"] == "pong"
        ws.send_bytes(frame_bytes(seq=4))
        ack = json.loads(ws.receive_text())
        assert ack == {**ack, "type": "ack", "seq": 4}
        status = _status(client)
        assert status["frame_source"]["kind"] == "external"
        assert status["frame_source"]["connected"] is True
        assert status["frame_source"]["clock_synced"] is True
        assert status["frame"] is not None
        assert status["camera"]["label"] == "Back Ultra Wide Camera"
    assert _status(client)["frame_source"]["connected"] is False


def test_sensor_messages_are_refused_on_the_camera_socket(client):
    with client.websocket_connect("/ws/camera", headers=AUTH) as ws:
        ws.send_text(as_json(gps_dict()))
        reply = json.loads(ws.receive_text())
        assert reply["type"] == "error"
        assert _status(client)["gps"] is None


def test_too_many_bad_messages_close_the_socket(client):
    with client.websocket_connect("/ws/camera", headers=AUTH) as ws:
        for _ in range(3):
            ws.send_text("not json")
            assert json.loads(ws.receive_text())["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == CLOSE_POLICY_VIOLATION


def test_newest_camera_takes_over(client):
    with client.websocket_connect("/ws/camera", headers=AUTH) as old:
        old.send_text(as_json(ping_dict()))
        old.receive_text()
        with client.websocket_connect("/ws/camera", headers=AUTH) as new:
            new.send_text(as_json(ping_dict()))
            assert json.loads(new.receive_text())["type"] == "pong"
            old.send_text(as_json(ping_dict()))
            with pytest.raises(WebSocketDisconnect) as closed:
                old.receive_text()
            assert closed.value.code == CLOSE_CAMERA_REPLACED


def test_phone_frames_are_not_shown_with_an_external_camera(client):
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_bytes(frame_bytes(seq=1))
        assert json.loads(phone.receive_text())["seq"] == 1
        assert _status(client)["frame"] is None


@pytest.mark.parametrize("token", ["short", "replace-with-a-real-token-please"])
def test_weak_or_example_tokens_are_refused_at_startup(token):
    with pytest.raises(ValidationError):
        camera_settings(token=SecretStr(token))


def test_blank_token_means_none():
    assert camera_settings(token=SecretStr("   ")).token is None
