"""/ws/model: every camera frame pushed to the driving model the moment it lands."""

import json
import struct
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from drive_service import sockets
from drive_service.app import create_app
from drive_service.config import Settings
from drive_service.model_feed import model_frame_bytes
from drive_service.protocol import Frame, FrameHeader
from drive_service.state import Stamped
from helpers import TINY_JPEG, as_json, frame_bytes, hello_dict, ping_dict

HOST = "testserver"
SAME_ORIGIN = {"origin": f"http://{HOST}"}
# Slow enough that a frame arriving before the next tick proves it was pushed, not polled.
SLOW_STATUS_FPS = 1.0
LENGTH = struct.Struct("<I")


def _split(message: bytes) -> tuple[dict, bytes]:
    (length,) = LENGTH.unpack_from(message)
    return json.loads(message[4 : 4 + length]), message[4 + length :]


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(sockets, "VIEWER_FPS", SLOW_STATUS_FPS)
    settings = Settings(
        record_dir=tmp_path / "recordings", web_dist=tmp_path / "missing", allowed_hosts=(HOST,)
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _synced_phone(phone: WebSocketTestSession) -> None:
    """Say hello and sync the clock, so frames get a capture time."""
    phone.send_text(as_json(hello_dict()))
    now_ms = time.time() * 1000
    phone.send_text(as_json(ping_dict(t=now_ms, rtt_ms=10.0, offset_ms=0.0)))
    assert json.loads(phone.receive_text())["type"] == "pong"


def _next_frame(model: WebSocketTestSession) -> tuple[dict, dict, bytes]:
    """Skip to the next frame; returns the status sent just before it, its header and JPEG."""
    status = None
    while True:
        message = model.receive()
        if message.get("text") is not None:
            status = json.loads(message["text"])
            continue
        header, jpeg = _split(message["bytes"])
        return status, header, jpeg


def test_header_carries_the_capture_age_on_the_service_clock() -> None:
    frame = Frame(header=FrameHeader(seq=4, t=1_000.0, w=480, h=270, rot=90), jpeg=TINY_JPEG)
    header, jpeg = _split(model_frame_bytes(Stamped(frame, 10.0, 9.95), now_mono=10.02))
    assert header == {"seq": 4, "t": 1_000.0, "w": 480, "h": 270, "rot": 90, "age_ms": 70.0}
    assert jpeg == TINY_JPEG


def test_pushes_every_frame_as_it_lands_not_on_a_timer(client: TestClient) -> None:
    with (
        client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone,
        client.websocket_connect("/ws/model") as model,
    ):
        model.receive_text()
        _synced_phone(phone)
        started = time.monotonic()
        seen = []
        for seq in range(3):
            phone.send_bytes(frame_bytes(seq=seq, t=time.time() * 1000))
            assert json.loads(phone.receive_text())["type"] == "ack"
            status, header, jpeg = _next_frame(model)
            seen.append(header["seq"])
            assert jpeg == TINY_JPEG
            # The status sent with a frame describes that frame.
            assert status["frame"]["seq"] == header["seq"]
        elapsed = time.monotonic() - started
    assert seen == [0, 1, 2]
    # Three frames well inside one status tick: each was pushed on arrival.
    assert elapsed < 0.5 / SLOW_STATUS_FPS


def test_ignores_what_the_model_sends(client: TestClient) -> None:
    with client.websocket_connect("/ws/model") as model:
        model.receive_text()
        model.send_text("hello?")
        with client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone:
            _synced_phone(phone)
            phone.send_bytes(frame_bytes(seq=7, t=time.time() * 1000))
            _, header, _ = _next_frame(model)
    assert header["seq"] == 7
