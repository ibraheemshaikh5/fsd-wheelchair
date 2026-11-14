"""/ws/model: every camera frame pushed to the driving model the moment it lands."""

import json
import struct
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from drive_service.app import create_app
from drive_service.config import Settings
from drive_service.model_feed import MAX_MODEL_FEEDS, model_frame_bytes
from drive_service.protocol import Frame, FrameHeader
from drive_service.state import Stamped
from helpers import TINY_JPEG, as_json, frame_bytes, hello_dict, ping_dict

HOST = "testserver"
SAME_ORIGIN = {"origin": f"http://{HOST}"}
OTHER_ORIGIN = {"origin": "https://evil.example"}
# Slow enough that a frame arriving before the next tick proves it was pushed, not polled.
SLOW_STATUS_FPS = 1.0
LENGTH = struct.Struct("<I")


def _split(message: bytes) -> tuple[dict, bytes]:
    (length,) = LENGTH.unpack_from(message)
    return json.loads(message[4 : 4 + length]), message[4 + length :]


def _settings(tmp_path: Path, viewer_fps: float = SLOW_STATUS_FPS) -> Settings:
    return Settings(
        record_dir=tmp_path / "recordings",
        web_dist=tmp_path / "missing",
        allowed_hosts=(HOST,),
        viewer_fps=viewer_fps,
        recorder_min_free_bytes=0,
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    with TestClient(create_app(_settings(tmp_path))) as test_client:
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


def test_header_has_no_age_before_the_clocks_are_synced() -> None:
    frame = Frame(header=FrameHeader(seq=0, t=1_000.0, w=480, h=270), jpeg=TINY_JPEG)
    header, _ = _split(model_frame_bytes(Stamped(frame, 10.0, None), now_mono=10.5))
    assert header["age_ms"] is None
    assert "rot" not in header


def test_sends_the_status_at_once(client: TestClient) -> None:
    with client.websocket_connect("/ws/model") as model:
        assert json.loads(model.receive_text())["type"] == "status"


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


def test_frame_age_is_known_once_the_clock_is_synced(client: TestClient) -> None:
    with (
        client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone,
        client.websocket_connect("/ws/model") as model,
    ):
        model.receive_text()
        _synced_phone(phone)
        phone.send_bytes(frame_bytes(seq=0, t=time.time() * 1000))
        _, header, _ = _next_frame(model)
    assert header["age_ms"] is not None
    assert 0.0 <= header["age_ms"] < 1_000.0


def test_keeps_sending_the_status_while_no_frames_come(tmp_path: Path) -> None:
    with (
        TestClient(create_app(_settings(tmp_path, viewer_fps=50.0))) as client,
        client.websocket_connect("/ws/model") as model,
    ):
        kinds = [json.loads(model.receive_text())["type"] for _ in range(3)]
    assert kinds == ["status", "status", "status"]


def test_ignores_what_the_model_sends(client: TestClient) -> None:
    with client.websocket_connect("/ws/model") as model:
        model.receive_text()
        model.send_text("hello?")
        with client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone:
            _synced_phone(phone)
            phone.send_bytes(frame_bytes(seq=7, t=time.time() * 1000))
            _, header, _ = _next_frame(model)
    assert header["seq"] == 7


def test_model_feeds_have_their_own_limit(client: TestClient) -> None:
    assert MAX_MODEL_FEEDS == 2
    with (
        client.websocket_connect("/ws/model") as first,
        client.websocket_connect("/ws/model") as second,
    ):
        first.receive_text()
        second.receive_text()
        with client.websocket_connect("/ws/model") as third:
            with pytest.raises(WebSocketDisconnect) as closed:
                third.receive_text()
            assert closed.value.code == 4429
        # Viewers (the iPad) are counted apart, so they never lock the model out.
        with client.websocket_connect("/ws/viewer") as viewer:
            assert json.loads(viewer.receive_text())["type"] == "status"


def test_a_closed_feed_frees_its_slot(client: TestClient) -> None:
    for _ in range(MAX_MODEL_FEEDS + 1):
        with client.websocket_connect("/ws/model") as model:
            assert json.loads(model.receive_text())["type"] == "status"


def test_refuses_another_site(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/model", headers=OTHER_ORIGIN) as model,
    ):
        model.receive_text()
