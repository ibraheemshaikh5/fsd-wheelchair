"""Every binary frame is answered with an ack carrying its seq."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from drive_service.app import create_app
from drive_service.config import Settings
from drive_service.protocol import FrameHeader, encode_frame
from helpers import as_json, frame_bytes, ping_dict


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        record_dir=tmp_path / "recordings",
        web_dist=tmp_path / "missing",
        allowed_hosts=("testserver",),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _receive(phone: WebSocketTestSession) -> dict:
    return json.loads(phone.receive_text())


def _status(client: TestClient) -> dict:
    return client.get("/api/status").json()["data"]


def test_each_frame_gets_an_ack_in_order(client):
    with client.websocket_connect("/ws/phone") as phone:
        for seq in (3, 4, 7):
            phone.send_bytes(frame_bytes(seq=seq))
        acks = [_receive(phone) for _ in range(3)]
    assert [ack["type"] for ack in acks] == ["ack"] * 3
    assert [ack["seq"] for ack in acks] == [3, 4, 7]
    assert all(ack["v"] == 1 for ack in acks)
    assert all(isinstance(ack["t"], float) and ack["t"] > 0 for ack in acks)
    assert set(acks[0]) == {"v", "type", "seq", "t"}


def test_refused_frame_with_a_readable_header_gets_an_error_then_an_ack(client):
    raw = encode_frame(FrameHeader(seq=12, t=1.0, w=640, h=480), b"GIF89a....")
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_bytes(raw)
        error = _receive(phone)
        ack = _receive(phone)
        assert _status(client)["stats"]["rejected"] == 1
    assert error["type"] == "error"
    assert "JPEG" in error["message"]
    assert ack["type"] == "ack"
    assert ack["seq"] == 12


def test_unparseable_frame_header_gets_only_an_error(client):
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_bytes(b"\x00")
        assert _receive(phone)["type"] == "error"
        phone.send_text(as_json(ping_dict(t=55.0)))
        # The next reply is the pong: no ack was sent for the refused frame.
        pong = _receive(phone)
    assert (pong["type"], pong["t"]) == ("pong", 55.0)
