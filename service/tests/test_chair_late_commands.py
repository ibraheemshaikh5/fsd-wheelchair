"""Commands that reach the board late must end the link instead of piling up.

Two ways the Mac-to-board direction can back up while TCP stays open:
the board stops reading (sends stall), or it reads but far behind (its
reported applied_seq trails the commands sent).
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from drive_service.chair import build_chair
from drive_service.chair.sockets import CLOSE_CHAIR_IDLE, CLOSE_CHAIR_LAGGING
from helpers import FakeTime
from test_chair_support import AUTH, as_json, chair_settings, hello_dict, make_app, status_dict

# Stall test: ttl 100 ms, so a send stuck longer than that ends the session.
STALL_TTL_MS = 100
STALL_BOUND_S = 2.0


class _StuckSocket:
    """A board whose TCP peer is open but never reads: every send and close hangs."""

    def __init__(self) -> None:
        self.headers = AUTH
        self.client = None
        self.sent = 0
        self.close_codes: list[int] = []

    async def accept(self) -> None:
        return None

    async def send_text(self, _text: str) -> None:
        self.sent += 1
        if self.sent > 1:
            await asyncio.Event().wait()

    async def receive(self) -> dict:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_codes.append(code)
        await asyncio.Event().wait()


def _chair_endpoint(chair):
    (route,) = chair.ws_router.routes
    return route.endpoint


def test_a_board_that_stops_reading_is_closed_within_the_ttl():
    settings = chair_settings(ttl_ms=STALL_TTL_MS, rate_hz=20)
    chair = build_chair(settings, (), FakeTime())
    ws = _StuckSocket()
    started = time.monotonic()
    asyncio.run(asyncio.wait_for(_chair_endpoint(chair)(ws), timeout=STALL_BOUND_S))
    assert time.monotonic() - started < 1.0
    assert ws.close_codes == [CLOSE_CHAIR_IDLE]
    link = chair.store.state
    assert link.connected is False
    assert link.bench is None


@pytest.fixture
def client():
    with TestClient(make_app(time_source=FakeTime())) as test_client:
        yield test_client


def _drive(ws) -> dict:
    return json.loads(ws.receive_text())


def _drain(ws, count: int) -> int:
    seq = -1
    for _ in range(count):
        seq = _drive(ws)["seq"]
    return seq


def _closed_code(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        while True:
            _drive(ws)
    return closed.value.code


def test_a_board_applying_old_commands_is_closed(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        chair.send_text(as_json(hello_dict()))
        first = _drain(chair, 1)
        _drain(chair, 15)  # 15 commands (750 ms) sent since the one the board applies
        chair.send_text(as_json(status_dict(applied_seq=first)))
        assert _closed_code(chair) == CLOSE_CHAIR_LAGGING
    link = client.get("/api/chair").json()["data"]
    assert link["connected"] is False
    assert link["test"]["active"] is False


def test_a_board_keeping_up_stays_connected(client):
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        chair.send_text(as_json(hello_dict()))
        for _ in range(15):
            seq = _drain(chair, 1)
            chair.send_text(as_json(status_dict(applied_seq=seq)))
        _drain(chair, 2)
        assert client.get("/api/chair").json()["data"]["connected"] is True


def test_a_stopped_board_reporting_no_applied_seq_stays_connected(client):
    # The sketch reports applied_seq null whenever it is stopped (e-stop, watchdog, unarmed).
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        chair.send_text(as_json(hello_dict()))
        _drain(chair, 15)
        chair.send_text(as_json(status_dict(applied_seq=None, estop=True)))
        _drain(chair, 2)
        assert client.get("/api/chair").json()["data"]["connected"] is True


def test_an_applied_seq_from_the_future_is_not_lag(client):
    # After a service restart seq starts again at 0; a board may still report an older, higher one.
    with client.websocket_connect("/ws/chair", headers=AUTH) as chair:
        chair.send_text(as_json(hello_dict()))
        _drain(chair, 2)
        chair.send_text(as_json(status_dict(applied_seq=1_000_000)))
        _drain(chair, 2)
        assert client.get("/api/chair").json()["data"]["connected"] is True
