"""End to end: a fake board against a test app holding only the chair routers."""

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from scripts import fake_chair
from scripts.fake_chair import BoardInputs, FakeBoard, apply_drive, hello_message, status_message
from test_chair_support import AUTH, SAME_ORIGIN, TOKEN, make_app

SERVER_START_TIMEOUT_S = 5.0


def _answer(chair, board: FakeBoard, ticks: int) -> FakeBoard:
    """Act as the board for a few ticks: apply each command, report status."""
    for _ in range(ticks):
        text = chair.receive_text()
        now_ms = time.monotonic() * 1000.0
        board = apply_drive(board, text, now_ms)
        chair.send_text(status_message(board, BoardInputs(), now_ms, time.time() * 1000.0))
    return board


def test_fake_board_follows_a_bench_command_through_the_test_client():
    with (
        TestClient(make_app()) as client,
        client.websocket_connect("/ws/chair", headers=AUTH) as chair,
    ):
        board = FakeBoard(max_speed_mps=0.5, max_turn_radps=0.5)
        chair.send_text(hello_message(board))
        board = _answer(chair, board, 3)
        body = {"v_mps": 0.25, "w_radps": 0.1, "duration_ms": 2000}
        assert client.post("/api/chair/test", json=body, headers=SAME_ORIGIN).status_code == 200
        board = _answer(chair, board, 4)
        status = client.get("/api/chair").json()["data"]["status"]
        assert status["output"] == {"v_mps": 0.25, "w_radps": 0.1}
        assert status["applied_seq"] == board.applied_seq
        assert client.delete("/api/chair/test", headers=SAME_ORIGIN).status_code == 200
        _answer(chair, board, 3)
        status = client.get("/api/chair").json()["data"]["status"]
        assert status["output"] == {"v_mps": 0.0, "w_radps": 0.0}


@pytest.fixture
def live_server() -> Iterator[str]:
    """The test app on a real loopback port, so the fake chair's websockets client works."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(make_app(), log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - only when the server breaks
            raise RuntimeError("test server did not start")
        time.sleep(0.01)
    yield f"127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=SERVER_START_TIMEOUT_S)
    sock.close()


async def _bench_while_running(base: str, config: fake_chair.FakeChairConfig) -> dict:
    task = asyncio.create_task(fake_chair.run(config))
    async with httpx.AsyncClient(base_url=f"http://{base}") as http:
        await asyncio.sleep(0.4)
        body = {"v_mps": 0.2, "w_radps": -0.2, "duration_ms": 2000}
        started = await http.post("/api/chair/test", json=body)
        assert started.status_code == 200, started.text
        await asyncio.sleep(0.3)
        seen = (await http.get("/api/chair")).json()["data"]
        code = await task
        after = (await http.get("/api/chair")).json()["data"]
    return {"seen": seen, "code": code, "after": after}


def test_fake_chair_script_against_a_live_server(live_server):
    config = fake_chair.FakeChairConfig(
        url=f"ws://{live_server}/ws/chair", token=TOKEN, seconds=1.0
    )
    result = asyncio.run(_bench_while_running(live_server, config))
    seen = result["seen"]
    assert seen["connected"] is True
    assert seen["hello"]["firmware"] == fake_chair.FIRMWARE
    assert seen["status"]["output"] == {"v_mps": 0.2, "w_radps": -0.2}
    assert seen["status"]["mode"] == "auto"
    assert result["code"] == 0
    assert result["after"]["connected"] is False


def test_fake_chair_reports_a_refused_token(live_server):
    config = fake_chair.FakeChairConfig(
        url=f"ws://{live_server}/ws/chair", token="wrong-token-0123456789", seconds=1.0
    )
    assert asyncio.run(fake_chair.run(config)) == fake_chair.EXIT_REFUSED


def test_fake_chair_reports_an_unreachable_service():
    config = fake_chair.FakeChairConfig(url="ws://127.0.0.1:9/ws/chair", token=TOKEN, seconds=1.0)
    assert asyncio.run(fake_chair.run(config)) == fake_chair.EXIT_UNREACHABLE


def test_fake_chair_with_estop_pressed_never_moves(live_server):
    config = fake_chair.FakeChairConfig(
        url=f"ws://{live_server}/ws/chair",
        token=TOKEN,
        seconds=0.6,
        inputs=BoardInputs(estop=True),
    )

    async def scenario() -> dict:
        task = asyncio.create_task(fake_chair.run(config))
        async with httpx.AsyncClient(base_url=f"http://{live_server}") as http:
            await asyncio.sleep(0.3)
            refused = await http.post(
                "/api/chair/test", json={"v_mps": 0.2, "w_radps": 0.0, "duration_ms": 500}
            )
            data = (await http.get("/api/chair")).json()["data"]
        await task
        return {"refused": refused.status_code, "data": data}

    result = asyncio.run(scenario())
    assert result["refused"] == 409
    assert result["data"]["status"]["estop"] is True
    assert result["data"]["status"]["stopped_reason"] == "estop"


def test_main_reads_the_token_from_the_named_environment_variable(monkeypatch):
    captured = {}

    async def fake_run(config):
        captured["config"] = config
        return 0

    monkeypatch.setattr(fake_chair, "run", fake_run)
    monkeypatch.setenv("MY_CHAIR_TOKEN", TOKEN)
    with pytest.raises(SystemExit) as exited:
        fake_chair.main(["--token-env", "MY_CHAIR_TOKEN", "--seconds", "2", "--estop"])
    assert exited.value.code == 0
    assert captured["config"].token == TOKEN
    assert captured["config"].inputs.estop is True
    assert captured["config"].seconds == 2.0


def test_main_refuses_a_missing_token(monkeypatch):
    monkeypatch.delenv("NOT_SET_CHAIR_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exited:
        fake_chair.main(["--token-env", "NOT_SET_CHAIR_TOKEN"])
    assert exited.value.code == 2


def test_status_messages_round_trip_as_json():
    board = FakeBoard(max_speed_mps=0.5, max_turn_radps=0.5)
    message = json.loads(status_message(board, BoardInputs(), 0.0, 1.0))
    assert message["type"] == "chair"
