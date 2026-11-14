"""The chair link is part of the real app: its routes exist and the app's guards apply."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from drive_service.app import create_app
from drive_service.chair import ChairSettings
from drive_service.config import Settings

TOKEN = "wiring-test-token-0123456789"
HOST = {"host": "localhost"}


def _app(tmp_path: Path, token: str | None = TOKEN, control_hz: int = 5):
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        max_control_rate_hz=control_hz,
    )
    return create_app(settings, chair_settings=ChairSettings(token=token))


def test_chair_status_route_is_served(tmp_path: Path):
    with TestClient(_app(tmp_path)) as client:
        reply = client.get("/api/chair", headers=HOST)
    assert reply.status_code == 200
    assert reply.json()["success"] is True
    assert reply.json()["data"]["connected"] is False


def test_chair_socket_accepts_the_configured_token(tmp_path: Path):
    with TestClient(_app(tmp_path)) as client:
        headers = {**HOST, "authorization": f"Bearer {TOKEN}"}
        with client.websocket_connect("/ws/chair", headers=headers) as ws:
            assert ws.receive_json()["type"] == "drive"


def test_chair_socket_is_refused_without_a_configured_token(tmp_path: Path):
    with TestClient(_app(tmp_path, token=None)) as client:
        headers = {**HOST, "authorization": f"Bearer {TOKEN}"}
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws/chair", headers=headers) as ws,
        ):
            ws.receive_json()


def test_bench_test_from_another_site_is_refused_before_the_control_budget(tmp_path: Path):
    foreign = {**HOST, "origin": "https://evil.example"}
    same = {**HOST, "origin": "http://localhost"}
    with TestClient(_app(tmp_path, control_hz=2)) as client:
        refused = [client.delete("/api/chair/test", headers=foreign).status_code for _ in range(5)]
        mine = client.delete("/api/chair/test", headers=same)
    assert refused == [403] * 5
    assert mine.status_code == 200


def test_bench_tests_share_the_control_rate_budget(tmp_path: Path):
    same = {**HOST, "origin": "http://localhost"}
    with TestClient(_app(tmp_path, control_hz=2)) as client:
        codes = [client.delete("/api/chair/test", headers=same).status_code for _ in range(4)]
    assert 429 in codes


DRIVER_TOKEN = "wiring-driver-token-0123456789"


def _driving_app(tmp_path: Path, control_hz: int = 2, drive_hz: int = 30):
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        max_control_rate_hz=control_hz,
        max_drive_rate_hz=drive_hz,
    )
    chair = ChairSettings(token=TOKEN, driver_token=DRIVER_TOKEN)
    return create_app(settings, chair_settings=chair)


def test_model_commands_have_their_own_budget(tmp_path: Path):
    driver = {**HOST, "authorization": f"Bearer {DRIVER_TOKEN}"}
    same = {**HOST, "origin": "http://localhost"}
    with TestClient(_driving_app(tmp_path)) as client:
        drives = [client.delete("/api/chair/drive", headers=driver).status_code for _ in range(10)]
        # Ten model stops spent none of the control budget.
        bench = client.delete("/api/chair/test", headers=same)
    assert drives == [200] * 10
    assert bench.status_code == 200


def test_model_command_budget_is_limited(tmp_path: Path):
    driver = {**HOST, "authorization": f"Bearer {DRIVER_TOKEN}"}
    with TestClient(_driving_app(tmp_path, drive_hz=3)) as client:
        codes = [client.delete("/api/chair/drive", headers=driver).status_code for _ in range(6)]
    assert 429 in codes
