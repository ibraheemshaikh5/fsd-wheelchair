"""The console and the chair: when it stops, and who may move it."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from fsd_ml.chair_link import SendResult
from fsd_ml.console.server import Session, make_server
from fsd_ml.drivers import FanDriver

NOWHERE = "http://127.0.0.1:9"  # nothing listens there


class FakeSender:
    def __init__(self, halted: bool = False) -> None:
        self.calls: list[tuple] = []
        self.halted = halted

    def engage(self) -> None:
        self.calls.append(("engage",))

    def release(self) -> None:
        self.calls.append(("release",))

    def send_stick(self, x: float, y: float, reverse: bool = False) -> SendResult:
        self.calls.append(("stick", round(x), round(y)))
        if self.halted:
            return SendResult(False, "the rider took over", halted=True)
        return SendResult(True, x=round(x), y=round(y))

    def brake(self, pct: float, ms: float) -> SendResult:
        self.calls.append(("stop",))
        return SendResult(True)

    def telemetry(self) -> dict:
        return {"sent": (0, 0), "takeover": False}


def driving(tmp_path: Path, sender: FakeSender) -> Session:
    """A session whose model is switched on to drive, with a fake chair."""
    session = Session(FanDriver(), tmp_path, "fan")
    session.connect_live(NOWHERE)
    session._sender, session.sending = sender, True
    return session


def test_with_nothing_to_decide_on_the_chair_is_stopped(tmp_path: Path) -> None:
    fake = FakeSender()
    session = driving(tmp_path, fake)
    waiting = session.live_decide((10.0, 0.0), 1.0, "point", "")
    assert "waiting" in waiting and fake.calls == [("stop",)]
    # Switching off stops it again and hands the chair back to its own joystick.
    assert session.stop()["sending"] is False
    assert fake.calls == [("stop",), ("stop",), ("release",)] and not session.sending


def test_a_takeover_switches_sending_off(tmp_path: Path) -> None:
    fake = FakeSender(halted=True)
    session = driving(tmp_path, fake)
    result = session._send_decision({"v_mps": 0.4, "w_radps": 0.0}, None, {})
    assert result["halted"] is True and not session.sending
    assert fake.calls[-1] == ("release",)
    # The same from the keyboard or a set move.
    session._sender, session.sending, session._armed = fake, True, True
    assert session.drive(0, 10) == "the rider took over"
    assert not session.sending and not session.armed()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[httpx.Client]:
    srv = make_server(Session(FanDriver(), tmp_path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_address[1]}", timeout=30) as c:
        yield c
    srv.shutdown()
    srv.server_close()


MOVES = [
    "/api/live/chair?on=1",
    "/api/live/manual?on=1",
    "/api/live/drive?x=0&y=10",
    "/api/live/decide?gx=5&gy=0&speed=0",
    "/api/bench/run?case=straight",
    "/api/bench/set?name=drive_stick_pct&value=30",
]


@pytest.mark.parametrize("route", MOVES)
def test_only_the_consoles_own_page_can_move_the_chair(client: httpx.Client, route: str) -> None:
    # Another site, and a script that names no origin at all, are both refused.
    assert client.post(route, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(route).status_code == 403
    # The page itself gets past the check (and is then told to connect first, or the like).
    page = {"Origin": str(client.base_url).rstrip("/")}
    assert client.post(route, headers=page).status_code != 403


def test_anyone_can_stop_and_nothing_runs_unarmed(client: httpx.Client, tmp_path: Path) -> None:
    for stop in ("/api/live/chair?on=0", "/api/bench/stop", "/api/live/stop"):
        assert client.post(stop).status_code == 200
    session = Session(FanDriver(), tmp_path, "fan")
    with pytest.raises(RuntimeError, match="not armed"):
        session.runner.straight(1.0, background=False)
    with pytest.raises(RuntimeError, match="not armed"):
        session.runner.turn(90, background=False)


def test_a_run_is_one_whole_episode_in_the_record(tmp_path: Path) -> None:
    session = driving(tmp_path, FakeSender())
    session.sight = None  # no obstacle sense: nothing to stop for
    session.page_alive = lambda: True
    session._armed = True
    session.episode = session.runlog.episode(kind="straight", task=None)
    done = session.runner.straight(0.2, background=False)
    assert done["outcome"] == "finished" and session.episode is None

    lines = (session.runlog.folder / "episode-001.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert all({"t", "mono", "type"} <= set(r) for r in records)
    kinds = [r["type"] for r in records]
    assert kinds[0] == "episode_start" and kinds[-1] == "episode_end"
    assert kinds.index("action_start") < kinds.index("tick") < kinds.index("action_end")
    assert records[0]["code"]["commit"] and "settings" not in records[0]  # as given above
    # Every stick command is there, with the believed pose moved on by it.
    ticks = [r for r in records if r["type"] == "tick"]
    assert ticks[-1]["pose"]["x_m"] == pytest.approx(0.2, abs=0.08)
    logged = [json.loads(line) for line in (session.runlog.folder / "log.jsonl").open()]
    commands = [line for line in logged if line["event"] == "command"]
    assert commands[-1]["pose"] == ticks[-1]["pose"] and "yaw_deg" in commands[-1]
    assert {"stick", "frame", "yaw_deg", "gyro_fresh", "camera_fresh"} <= set(ticks[0])
    # With no camera at all, that is warned of, not passed over.
    assert "warning" in kinds
    ended = next(r for r in records if r["type"] == "action_end")
    assert ended["ended"] == "finished" and ended["planned"] == {"distance_m": 0.2}
    assert ended["achieved"]["believed_m"] == pytest.approx(0.2, abs=0.08)
    assert records[-1]["success"] is None and records[-1]["totals"]["actions"] == 1
    # No camera frame came at all: the episode says so, and that it is no use as data.
    assert records[-1]["coverage"]["camera_fresh"] == 0 and records[-1]["usable"] is False
    session.sending = True
    assert "CAMERA STOPPED" in session.bench_state()["alert"]
