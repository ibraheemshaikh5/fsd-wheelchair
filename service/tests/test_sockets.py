"""Phone socket races that the TestClient cannot schedule on purpose."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from drive_service.config import Settings
from drive_service.hub import Hub
from drive_service.sockets import CLOSE_PHONE_REPLACED, _serve_phone
from helpers import FakeTime, NullRecorder, as_json, gps_dict


class FakeSocket:
    """Delivers queued messages, records closes, and can fail on close."""

    def __init__(self, messages: list[dict[str, Any]], close_error: Exception | None = None):
        self._messages = list(messages)
        self._close_error = close_error
        self.closes: list[int] = []
        self.sent: list[str] = []

    async def receive(self) -> dict[str, Any]:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.Event().wait()  # nothing more; wait until cancelled
        raise AssertionError("unreachable")

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int, reason: str) -> None:
        self.closes.append(code)
        if self._close_error is not None:
            raise self._close_error


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(record_dir=tmp_path, web_dist=tmp_path / "missing")


def _gps_message() -> dict[str, Any]:
    return {"type": "websocket.receive", "text": as_json(gps_dict(lat=-1.0))}


def test_message_queued_on_a_replaced_socket_is_never_applied(settings):
    # The takeover happened, but the old task woke up for a message before it saw
    # the superseded signal: the lease check must still stop the dispatch.
    hub = Hub(settings, NullRecorder(), FakeTime())
    old = hub.attach_phone()
    hub.attach_phone()
    ws = FakeSocket([_gps_message()])
    asyncio.run(_serve_phone(ws, hub, settings, old, asyncio.Event()))
    assert ws.closes == [CLOSE_PHONE_REPLACED]
    assert hub.snapshot.gps is None
    assert hub.snapshot.phone_connected is True


def test_closing_an_already_gone_socket_is_not_an_error(settings):
    hub = Hub(settings, NullRecorder(), FakeTime())
    old = hub.attach_phone()
    hub.attach_phone()
    ws = FakeSocket([_gps_message()], close_error=RuntimeError("already closed"))
    asyncio.run(_serve_phone(ws, hub, settings, old, asyncio.Event()))
    assert ws.closes == [CLOSE_PHONE_REPLACED]
