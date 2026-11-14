"""Every binary frame is answered with an ack carrying its seq, and link stats reach the status."""

import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from drive_service.app import create_app
from drive_service.config import Settings
from drive_service.protocol import FrameHeader, encode_frame
from helpers import TINY_JPEG, as_json, frame_bytes, hello_dict, link_dict, ping_dict

HOST = "testserver"
SAME_ORIGIN = {"origin": f"http://{HOST}"}


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "record_dir": tmp_path / "recordings",
        "web_dist": tmp_path / "missing",
        "allowed_hosts": (HOST,),
        "recorder_min_free_bytes": 0,
        "max_consecutive_errors": 3,
    }
    return Settings(**{**base, **overrides})


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return _settings(tmp_path)


@pytest.fixture
def client(settings) -> TestClient:
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


def test_rate_limited_frames_are_still_acked(tmp_path):
    settings = _settings(tmp_path, max_frame_rate_hz=1)
    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/ws/phone") as phone,
    ):
        for seq in range(4):
            phone.send_bytes(frame_bytes(seq=seq))
        acks = [_receive(phone) for _ in range(4)]
        status = _status(client)
    assert [ack["seq"] for ack in acks] == [0, 1, 2, 3]
    assert status["stats"]["rate_limited"] == 3


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


def test_oversized_frame_with_a_readable_header_is_acked(tmp_path):
    settings = _settings(tmp_path, max_frame_bytes=40)
    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/ws/phone") as phone,
    ):
        phone.send_bytes(frame_bytes(seq=5))
        assert _receive(phone)["type"] == "error"
        ack = _receive(phone)
    assert (ack["type"], ack["seq"]) == ("ack", 5)


def _raw_frame(header: dict) -> bytes:
    header_bytes = json.dumps(header).encode()
    return struct.pack("<I", len(header_bytes)) + header_bytes + TINY_JPEG


def test_invalid_header_with_a_readable_seq_gets_an_error_then_an_ack(client):
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_bytes(_raw_frame({"seq": 7, "t": 1.0, "w": 0, "h": 10}))
        error = _receive(phone)
        ack = _receive(phone)
    assert error["type"] == "error"
    assert "w" in error["message"]
    assert (ack["type"], ack["seq"]) == ("ack", 7)


def test_header_with_an_added_field_is_refused_but_acked(client):
    header = {"seq": 8, "t": 1.0, "w": 640, "h": 480, "exposure_ms": 4}
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_bytes(_raw_frame(header))
        assert _receive(phone)["type"] == "error"
        ack = _receive(phone)
    assert (ack["type"], ack["seq"]) == ("ack", 8)


def test_unparseable_frame_header_gets_only_an_error(client):
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_bytes(b"\x00")
        assert _receive(phone)["type"] == "error"
        phone.send_text(as_json(ping_dict(t=55.0)))
        # The next reply is the pong: no ack was sent for the refused frame.
        pong = _receive(phone)
    assert (pong["type"], pong["t"]) == ("pong", 55.0)


def test_acks_for_refused_frames_do_not_reset_the_error_streak(client):
    raw = encode_frame(FrameHeader(seq=1, t=1.0, w=640, h=480), b"GIF89a....")
    with client.websocket_connect("/ws/phone") as phone:
        for _ in range(3):
            phone.send_bytes(raw)
            assert _receive(phone)["type"] == "error"
            assert _receive(phone)["type"] == "ack"
        with pytest.raises(WebSocketDisconnect) as closed:
            phone.receive_text()
        assert closed.value.code == 1008


def test_link_message_gets_no_reply_and_reaches_the_status(client):
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_text(as_json(link_dict(sent=250, ack_rtt_ms=None)))
        phone.send_text(as_json(ping_dict()))
        assert _receive(phone)["type"] == "pong"
        link = _status(client)["link"]
    assert link["sent"] == 250
    assert link["ack_rtt_ms"] is None
    assert link["fresh"] is True
    assert link["age_ms"] >= 0


def test_status_link_is_null_before_any_link_message(client):
    assert _status(client)["link"] is None


def test_link_message_is_recorded_in_sensors_jsonl(client, settings):
    with client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone:
        phone.send_text(as_json(hello_dict()))
        phone.send_text(as_json(ping_dict()))
        _receive(phone)
        session = client.post("/api/recording/start", headers=SAME_ORIGIN).json()["data"]["session"]
        phone.send_text(as_json(link_dict()))
        phone.send_text(as_json(ping_dict()))
        _receive(phone)
    stopped = client.post("/api/recording/stop", headers=SAME_ORIGIN).json()["data"]
    lines = (settings.record_dir / session / "sensors.jsonl").read_text().splitlines()
    assert stopped["sensors"] == 1
    recorded = json.loads(lines[0])
    assert recorded["type"] == "link"
    assert recorded["recv_ms"] > 0
    assert {key: value for key, value in recorded.items() if key != "recv_ms"} == link_dict()
