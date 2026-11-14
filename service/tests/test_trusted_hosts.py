"""Host allowlist: a DNS-rebinding page must not reach the API, sockets or pages."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.testclient import WebSocketDenialResponse

from drive_service.app import create_app
from drive_service.config import DEFAULT_ALLOWED_HOSTS, Settings
from helpers import as_json, ping_dict

TEST_HOSTS = ("testserver",)
REBOUND_HOST = "evil.example:8000"
REBOUND = {"host": REBOUND_HOST, "origin": f"http://{REBOUND_HOST}"}
BAD_REQUEST = 400


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = {"record_dir": tmp_path / "rec", "web_dist": tmp_path / "missing"}
    return Settings(**{**defaults, **overrides})


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    with TestClient(create_app(_settings(tmp_path, allowed_hosts=TEST_HOSTS))) as test_client:
        yield test_client


def _status(client: TestClient) -> dict:
    response = client.get("/api/status")
    assert response.status_code == 200
    return response.json()["data"]


@pytest.mark.parametrize("path", ["/ws/viewer", "/ws/phone"])
def test_rebound_host_is_refused_on_sockets(client, path):
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(path, headers=dict(REBOUND)),
    ):
        pass
    assert denied.value.status_code == BAD_REQUEST
    assert _status(client)["phone_connected"] is False


def test_rebound_host_cannot_start_a_recording(client):
    response = client.post("/api/recording/start", headers=dict(REBOUND))
    assert response.status_code == BAD_REQUEST
    assert _status(client)["recording"]["active"] is False


def test_rebound_host_cannot_read_status(client):
    response = client.get("/api/status", headers={"host": REBOUND_HOST})
    assert response.status_code == BAD_REQUEST
    assert "gps" not in response.text


def test_rebound_host_cannot_load_pages(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>chair</h1>")
    settings = _settings(tmp_path, web_dist=dist, allowed_hosts=TEST_HOSTS)
    with TestClient(create_app(settings)) as client:
        assert client.get("/", headers={"host": REBOUND_HOST}).status_code == BAD_REQUEST
        assert "chair" in client.get("/").text


def test_lookalike_of_a_wildcard_host_is_refused(tmp_path):
    settings = _settings(tmp_path, allowed_hosts=("*.ts.net",))
    with TestClient(create_app(settings)) as client:
        for host in ("ts.net.evil.example", "evilts.net"):
            response = client.get("/api/status", headers={"host": host})
            assert response.status_code == BAD_REQUEST, host


@pytest.mark.parametrize(
    ("allowed_hosts", "headers"),
    [
        pytest.param(
            ("*.ts.net",),
            {"host": "chair.tail1234.ts.net", "origin": "https://chair.tail1234.ts.net"},
            id="tailscale-serve",
        ),
        pytest.param(DEFAULT_ALLOWED_HOSTS, {"host": "127.0.0.1:8000"}, id="fake-phone"),
        pytest.param(
            DEFAULT_ALLOWED_HOSTS,
            {"host": "localhost:5173", "origin": "http://localhost:5173"},
            id="vite-proxy",
        ),
    ],
)
def test_expected_hosts_are_accepted(tmp_path, allowed_hosts, headers):
    settings = _settings(tmp_path, allowed_hosts=allowed_hosts)
    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/ws/phone", headers=dict(headers)) as phone,
    ):
        phone.send_text(as_json(ping_dict(t=5.0)))
        assert json.loads(phone.receive_text())["type"] == "pong"
        status = client.get("/api/status", headers={"host": headers["host"]})
        assert status.status_code == 200
        assert status.json()["data"]["phone_connected"] is True


def test_default_hosts_cover_loopback_and_tailscale(tmp_path, monkeypatch):
    monkeypatch.delenv("DRIVE_ALLOWED_HOSTS", raising=False)
    hosts = _settings(tmp_path).allowed_hosts
    assert {"localhost", "127.0.0.1", "*.ts.net"} <= set(hosts)
    assert "testserver" not in hosts


def test_allowed_hosts_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_ALLOWED_HOSTS", '["a.b.ts.net"]')
    assert _settings(tmp_path).allowed_hosts == ("a.b.ts.net",)


def test_allowed_hosts_are_lowercased(tmp_path):
    assert _settings(tmp_path, allowed_hosts=("Chair.TS.net",)).allowed_hosts == ("chair.ts.net",)


@pytest.mark.parametrize(
    "allowed_hosts",
    [(), ("",), ("chair.*.ts.net",), ("*ts.net",), ("*",), (" localhost",)],
)
def test_malformed_allowed_hosts_are_rejected(tmp_path, allowed_hosts):
    with pytest.raises(ValidationError, match="allowed_hosts"):
        _settings(tmp_path, allowed_hosts=allowed_hosts)
