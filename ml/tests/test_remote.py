from __future__ import annotations

import threading
from collections.abc import Iterator

import httpx
import numpy as np
import pytest

from fsd_ml import remote
from fsd_ml.drivers import FanDriver, Goal, Motion, make_driver
from fsd_ml.sim.run import simulate
from fsd_ml.sim.scenarios import SCENARIOS


@pytest.fixture
def url() -> Iterator[str]:
    server = remote.make_server(FanDriver(), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_remote_driver_matches_the_local_one(url: str) -> None:
    frames = [np.full((216, 640, 3), 120, dtype=np.uint8)]
    goal, motion = Goal(point_xy=(5.0, 1.0)), Motion(1.0, 0.0)

    driver = make_driver(f"remote:{url}")
    far = driver.propose(frames, goal, motion)
    near = FanDriver().propose(frames, goal, motion)
    driver.close()

    assert (driver.name, driver.context_frames, driver.frame_hz) == ("remote-fan", 1, 20.0)
    assert [c.score for c in far] == [c.score for c in near]
    assert far[1].path_xy == pytest.approx(near[1].path_xy, abs=1e-3)
    assert driver.round_trip_ms >= driver.inference_ms >= 0.0


def test_frames_survive_the_trip_and_are_shrunk() -> None:
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[:, 320:] = 255

    back = remote._decode(remote._encode(frame))

    assert back.shape == (216, 384, 3)
    assert back[100, 10].max() < 20
    assert back[100, 370].min() > 235


def test_token_is_required_when_set(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(remote.TOKEN_ENV, "secret")

    with pytest.raises(httpx.HTTPStatusError):
        remote.RemoteDriver(url, token="wrong")
    assert remote.RemoteDriver(url).name == "remote-fan"  # picks the token up from the environment


def test_simulation_runs_through_a_remote_driver(url: str) -> None:
    result = simulate(SCENARIOS["pedestrian"], f"remote:{url}", real_timing=True)

    assert result.arrived
    assert not result.collided


def test_goal_photo_and_instruction_reach_the_driver() -> None:
    seen = {}

    class Recorder(FanDriver):
        def propose(self, frames, goal, motion):
            seen["goal"] = goal
            return super().propose(frames, Goal(point_xy=(5.0, 0.0)), motion)

    server = remote.make_server(Recorder(), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    driver = remote.RemoteDriver(f"http://127.0.0.1:{server.server_address[1]}")
    photo = np.full((90, 160, 3), 200, dtype=np.uint8)
    frame = np.zeros((90, 160, 3), dtype=np.uint8)

    driver.propose([frame], Goal(instruction="go to the door", image=photo), Motion())
    driver.close()
    server.shutdown()
    server.server_close()

    assert seen["goal"].point_xy is None
    assert seen["goal"].instruction == "go to the door"
    assert seen["goal"].image.shape == (90, 160, 3)
    assert seen["goal"].image.mean() > 190
