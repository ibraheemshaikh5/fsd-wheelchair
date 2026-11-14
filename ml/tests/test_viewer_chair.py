"""The live view can send its decisions to the chair through hardware/wheelchair.py."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from fsd_ml.chair_out import ChairOutError, SendResult
from fsd_ml.drivers import FanDriver
from fsd_ml.viewer import server
from fsd_ml.viewer.server import Session, ViewerError, make_server, same_origin

NOWHERE = "http://127.0.0.1:9"  # nothing listens there


class FakeSender:
    def __init__(self, halted: bool = False) -> None:
        self.calls: list[tuple] = []
        self.closed = False
        self.halted = halted

    def send(self, v_mps: float, w_radps: float) -> SendResult:
        self.calls.append(("send", v_mps, w_radps))
        if self.halted:
            return SendResult(False, v_mps, w_radps, "the rider took over", halted=True)
        return SendResult(True, v_mps, w_radps)

    def stop(self) -> SendResult:
        self.calls.append(("stop",))
        return SendResult(True, 0.0, 0.0)

    def close(self) -> None:
        self.closed = True


def test_sending_needs_a_live_connection_and_a_chair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    with pytest.raises(ViewerError, match="connect"):
        session.send_to_chair(True)
    session.connect_live(NOWHERE)

    def unreachable() -> FakeSender:
        raise ChairOutError("cannot reach the chair")

    monkeypatch.setattr(server, "ChairSender", unreachable)
    with pytest.raises(ViewerError, match="cannot reach"):
        session.send_to_chair(True)
    fake = FakeSender()
    monkeypatch.setattr(server, "ChairSender", lambda: fake)
    assert session.send_to_chair(True) == {"sending": True}
    assert session.chair_out is fake
    session.disconnect_live()
    assert session.chair_out is None and fake.closed


def test_waiting_sends_a_stop_and_turning_off_stops(tmp_path: Path) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    session.connect_live(NOWHERE)
    fake = FakeSender()
    session.chair_out = fake
    waiting = session.live_decide((10.0, 0.0), 1.0, "point", "")
    assert "waiting" in waiting
    assert waiting["chair"]["sent"] is True
    assert fake.calls == [("stop",)]
    off = session.send_to_chair(False)
    assert off["sending"] is False
    assert fake.calls == [("stop",), ("stop",)] and fake.closed
    session.disconnect_live()


def test_a_decision_is_sent_as_speed_and_turn(tmp_path: Path) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    fake = FakeSender()
    session.chair_out = fake
    sent = session._to_chair({"v_mps": 0.4, "w_radps": -0.05})
    assert fake.calls == [("send", 0.4, -0.05)]
    assert sent["v_mps"] == 0.4
    session.chair_out = None
    assert session._to_chair({"v_mps": 0.4, "w_radps": 0.0}) is None


def test_a_takeover_switches_sending_off(tmp_path: Path) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    fake = FakeSender(halted=True)
    session.chair_out = fake
    result = session._to_chair({"v_mps": 0.4, "w_radps": 0.0})
    assert result["halted"] is True
    assert session.chair_out is None and fake.closed


def test_same_origin() -> None:
    assert same_origin({}) is True
    assert same_origin({"Origin": "http://127.0.0.1:8200", "Host": "127.0.0.1:8200"}) is True
    assert same_origin({"Origin": "https://evil.example", "Host": "127.0.0.1:8200"}) is False
    assert same_origin({"Origin": "http://127.0.0.1:8200"}) is False


@pytest.fixture
def client(tmp_path: Path) -> Iterator[httpx.Client]:
    srv = make_server(Session(FanDriver(), tmp_path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_address[1]}", timeout=30) as c:
        yield c
    srv.shutdown()
    srv.server_close()


def test_another_site_cannot_switch_sending_on(client: httpx.Client) -> None:
    refused = client.post("/api/live/chair?on=1", headers={"Origin": "https://evil.example"})
    assert refused.status_code == 403
    # This page may, and is told to connect first.
    mine = client.post("/api/live/chair?on=1")
    assert mine.status_code == 400 and "connect" in mine.json()["error"]
    assert client.post("/api/live/chair?on=0").json() == {"sending": False}
