"""The /ws/camera socket refuses a sender without the configured token."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from drive_service.app import create_app
from drive_service.camera import CameraSettings
from drive_service.camera.sockets import CLOSE_POLICY_VIOLATION
from drive_service.config import Settings

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


def test_refused_without_a_configured_token(tmp_path):
    app = create_app(_settings(tmp_path), camera_settings=camera_settings(token=None))
    with TestClient(app) as client:
        assert _refused(client, AUTH) == CLOSE_POLICY_VIOLATION


def test_refused_with_a_wrong_or_missing_token(client):
    assert _refused(client, {}) == CLOSE_POLICY_VIOLATION
    assert _refused(client, {"authorization": "Bearer wrong-token-0123456789"}) == 1008
