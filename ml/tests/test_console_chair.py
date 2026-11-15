"""The live view can send its decisions to the chair through hardware/wheelchair.py."""

from __future__ import annotations

import json
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


class RealModel(FanDriver):
    name = "model"


class FakeSender:
    def __init__(self, halted: bool = False) -> None:
        self.calls: list[tuple] = []
        self.closed = False
        self.halted = halted

    def send_stick(self, x: float, y: float) -> SendResult:
        self.calls.append(("stick", round(x), round(y)))
        if self.halted:
            return SendResult(False, 0.0, 0.0, "the rider took over", halted=True)
        return SendResult(True, 0.0, 0.0, x=round(x), y=round(y))

    def brake(self, pct: float, ms: float) -> SendResult:
        self.calls.append(("stop",) if not pct else ("brake", pct, ms))
        return SendResult(True, 0.0, 0.0)

    def telemetry(self) -> dict:
        return {"sent": (0, 0), "takeover": False}

    def close(self) -> None:
        self.closed = True


def test_sending_needs_a_live_connection_and_a_chair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    with pytest.raises(ViewerError, match="connect"):
        session.send_to_chair(True)
    session.connect_live(NOWHERE)
    # A stand-in never drives the chair.
    with pytest.raises(ViewerError, match="stand-in"):
        session.send_to_chair(True)
    session.driver = RealModel()

    def unreachable() -> FakeSender:
        raise ChairOutError("cannot reach the chair")

    monkeypatch.setattr(server, "ChairSender", unreachable)
    with pytest.raises(ViewerError, match="cannot reach"):
        session.send_to_chair(True)
    fake = FakeSender()
    monkeypatch.setattr(server, "ChairSender", lambda: fake)
    assert session.send_to_chair(True) == {"sending": True, "steps_left": None}
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
    assert fake.calls == [("stick", 3, 27)]
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


def test_only_the_steps_asked_for_are_sent_then_the_chair_is_stopped(tmp_path: Path) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    fake = FakeSender()
    session.chair_out, session.chair_steps = fake, 2
    decision = {"v_mps": 0.4, "w_radps": 0.0}
    assert session._to_chair(decision)["steps_left"] == 1
    assert session._to_chair(decision)["steps_left"] == 0
    done = session._to_chair(decision)
    assert done["halted"] is True and "done" in done["problem"]
    assert fake.calls == [("stick", 0, 27), ("stick", 0, 27), ("stop",)]
    assert session.chair_out is None and fake.closed


def test_the_speed_setting_slows_speed_and_turn_together(tmp_path: Path) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    fake = FakeSender()
    session.chair_out, session.chair_scale = fake, 0.5
    # Fast enough to hit the 30% cap: the setting still halves what is sent.
    session._to_chair({"v_mps": 1.5, "w_radps": -0.75})
    assert fake.calls == [("stick", 8, 15)]


def test_a_live_connection_keeps_a_record(tmp_path: Path) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    session.log("ignored before connecting")
    session.connect_live(NOWHERE)
    session.chair_out = FakeSender()
    session.send_to_chair(False)
    session.disconnect_live()
    (record,) = (tmp_path / "live").glob("*/log.jsonl")
    events = [json.loads(line)["event"] for line in record.read_text().splitlines()]
    assert events == ["connected", "sending off", "stop"]


def test_only_the_page_can_arm_and_nothing_runs_unarmed(client: httpx.Client) -> None:
    # A script (no Origin) cannot arm, nor can another site.
    assert client.post("/api/episode/arm?on=1").status_code == 403
    other = client.post("/api/episode/arm?on=1", headers={"Origin": "https://evil.example"})
    assert other.status_code == 403
    # Unarmed, a run is refused; so is a turn.
    refused = client.post("/api/episode/go", params={"say": "go to the pillow"})
    assert refused.status_code == 400 and "not armed" in refused.json()["error"]
    assert client.post("/api/episode/turn?deg=90").status_code == 400
    # Anyone may disarm or stop.
    assert client.post("/api/episode/arm?on=0").json() == {"armed": False}
    assert client.post("/api/episode/stop").json()["running"] is False
    assert client.get("/api/episode").json()["armed"] is False


def test_the_page_arms_only_once_connected_and_stop_disarms(tmp_path: Path) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    with pytest.raises(ViewerError, match="connect"):
        session.arm(True)
    session.connect_live(NOWHERE)
    assert session.arm(True) == {"armed": True}
    # A stand-in is still never driven: the run ends at once with the reason.
    done = session.runner.go("go to the pillow", background=False)
    assert done["outcome"] == "failed" and "stand-in" in done["reason"]
    session.arm(False)
    assert not session.armed()
    session.disconnect_live()


def test_only_the_bench_page_starts_a_test_and_anyone_can_stop(client: httpx.Client) -> None:
    assert client.get("/bench").status_code == 200
    state = client.get("/api/bench").json()
    assert [c["id"] for c in state["cases"]][:2] == ["pilot", "straight"]
    assert state["settings"]["drive_stick_pct"] > 0
    # A script cannot press Run or leave feedback in the person's name.
    assert client.post("/api/bench/run?case=straight").status_code == 403
    assert client.post("/api/bench/feedback", json={"run": "x"}).status_code == 403
    page = {"Origin": str(client.base_url).rstrip("/")}
    refused = client.post("/api/bench/run?case=straight", headers=page)
    assert refused.status_code == 400 and "not connected" in refused.json()["error"]
    assert client.post("/api/bench/run?case=nope", headers=page).status_code == 400
    assert client.post("/api/bench/stop").status_code == 200
    said = client.post(
        "/api/bench/feedback",
        headers=page,
        json={"run": "r1", "rating": "harsh", "measured": "26 in", "text": "jerky"},
    )
    assert said.status_code == 200
    assert client.get("/api/bench/view").json()["waiting"]
