"""A route run with no chair: check the harness, not the model.

    uv run python -m fsd_ml.route_sim            # every course
    uv run python -m fsd_ml.route_sim slalom     # one

The real console session, runner and route run (route_run.py) drive a simulated chair: a
sender that takes the stick, a phone that reports the gyro and sends frames, and an obstacle
sense that reports what a camera would see of a made-up room. In place of Gemini is a pilot
that knows the way and draws it on the picture as a model would: as points in the picture,
a few seconds late, a few pixels off. So this shows whether a route drawn on a picture is
driven where it was drawn, on the move, with the answer arriving late and the chair's
sense of where it is drifting; it says nothing about whether a model draws good routes.

The chair is not the one the harness believes in: it goes SPEED_SCALE as fast as believed,
veers to the right by itself, answers the stick late, and coasts when turning. The run is
in real time (a course takes about a minute). For each course it prints how it ended, how
long it took, how close the chair's body came to anything, and how far its believed
position had drifted from the true one.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import random
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from fsd_ml import chair as chair_setup
from fsd_ml.chair_link import SendResult
from fsd_ml.console.server import Session
from fsd_ml.drivers import FanDriver
from fsd_ml.pipeline.types import Obstacle
from fsd_ml.route_pilot import Plan, into

# How the simulated chair differs from the one the harness believes in.
SPEED_SCALE = 0.85
VEER_DPS = -1.1
STICK_DELAY_S = 0.2
TURN_LAG_S = 0.45
TURN_DPS_AT_12 = 27.0
TURN_DEAD_PCT = 5.0
# The stand-in pilot: how long it takes to answer, and how far off its points are.
ANSWER_S = 3.5
POINT_NOISE = 0.008
# A course is given up on after this long.
LIMIT_S = 150.0


def box(x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    """The outline of a box on the floor, as points 0.1 m apart."""
    xs, ys = np.arange(x0, x1 + 1e-9, 0.1), np.arange(y0, y1 + 1e-9, 0.1)
    sides = [
        np.column_stack([xs, np.full_like(xs, y0)]),
        np.column_stack([xs, np.full_like(xs, y1)]),
        np.column_stack([np.full_like(ys, x0), ys]),
        np.column_stack([np.full_like(ys, x1), ys]),
    ]
    return np.vstack(sides)


# A course: what stands in the room, and the way through it that the stand-in pilot knows.
COURSES: dict[str, dict[str, Any]] = {
    # Round the left of a chest, then hook right to stop in front of a step.
    "ottoman": {
        "things": [box(2.4, -1.0, 3.0, 0.3), box(-1.0, 1.9, 7.0, 2.0), box(5.6, -2.2, 5.7, 0.4)],
        "way": [(1.0, 0.45), (2.2, 1.05), (3.4, 1.1), (4.3, 0.6), (4.7, -0.2), (4.75, -0.9)],
        "face": (5.6, -0.9),
    },
    # Left of one thing, then right of the next.
    "slalom": {
        "things": [box(2.0, -1.2, 2.5, 0.2), box(4.4, 0.0, 4.9, 1.6)],
        "way": [
            (1.0, 0.5),
            (2.2, 1.15),
            (3.4, 0.5),
            (4.0, -0.7),
            (4.7, -1.2),
            (5.6, -1.0),
            (6.4, -0.3),
        ],
        "face": None,
    },
    # Through a gap 1.3 m wide in a wall.
    "doorway": {
        "things": [box(3.0, 0.65, 3.1, 3.0), box(3.0, -3.0, 3.1, -0.65)],
        "way": [(1.0, 0.0), (2.0, 0.0), (3.0, 0.0), (4.0, 0.0), (4.8, 0.0)],
        "face": None,
    },
}


@dataclass
class World:
    """The chair as it really is, and what stands in the room (true places, in metres)."""

    things: np.ndarray
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    rate_dps: float = 0.0
    # The stick as last sent, and those still on their way to the chair.
    stick: tuple[float, float] = (0.0, 0.0)
    coming: list[tuple[float, tuple[float, float]]] = field(default_factory=list)
    closest_m: float = math.inf
    lock: threading.Lock = field(default_factory=threading.Lock)

    def pose(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.yaw)

    def step(self, now: float, dt: float, body: chair_setup.Body, full_mps: float) -> None:
        while self.coming and self.coming[0][0] <= now:
            self.stick = self.coming.pop(0)[1]
        sideways, forward = self.stick
        speed = SPEED_SCALE * forward / 100 * full_mps
        if forward:  # driving: a small sideways stick is a small turn, and the chair veers
            want = -sideways * 0.8 + VEER_DPS
        else:  # standing: the turn dead zone, then in proportion
            push = abs(sideways) - TURN_DEAD_PCT
            gain = TURN_DPS_AT_12 / (12 - TURN_DEAD_PCT)
            want = -math.copysign(gain * push, sideways) if push > 0 else 0.0
        self.rate_dps += (want - self.rate_dps) * dt / TURN_LAG_S
        # The point that drives and turns is the middle of the drive wheels, behind the
        # chair's own middle (x, y).
        behind = body.pivot_behind_m
        axle_x = self.x - behind * math.cos(self.yaw) + speed * math.cos(self.yaw) * dt
        axle_y = self.y - behind * math.sin(self.yaw) + speed * math.sin(self.yaw) * dt
        self.yaw += math.radians(self.rate_dps) * dt
        self.x = axle_x + behind * math.cos(self.yaw)
        self.y = axle_y + behind * math.sin(self.yaw)
        ahead, left = into(self.pose(), self.things).T
        gap = np.maximum(np.abs(ahead) - body.length_m / 2, np.abs(left) - body.width_m / 2)
        self.closest_m = min(self.closest_m, float(gap.min()))


class Sender:
    """Stands in for chair_link.ChairSender: the stick goes to the simulated chair."""

    def __init__(self, world: World) -> None:
        self._world = world

    def engage(self) -> None: ...

    def release(self) -> None:
        self._send(0.0, 0.0)

    def _send(self, x: float, y: float) -> SendResult:
        self._world.coming.append((time.monotonic() + STICK_DELAY_S, (x, y)))
        return SendResult(True, x=round(x), y=round(y))

    def send_stick(self, x: float, y: float, reverse: bool = False) -> SendResult:
        return self._send(float(x), float(y))

    def brake(self, pct: float, ms: float) -> SendResult:
        return self._send(0.0, 0.0)

    def telemetry(self) -> dict[str, Any]:
        return {"takeover": False}


class Sight:
    """Stands in for obstacles.ObstacleWatcher: what a camera on the chair would see."""

    def __init__(self, world: World, setup: chair_setup.ChairSetup) -> None:
        self._world, self._setup = world, setup
        self.obstacles: tuple[Obstacle, ...] = ()
        self.info: dict[str, Any] = {"floor": True}
        self.at_s = 0.0

    def stop(self) -> None: ...

    def age_s(self) -> float:
        return time.monotonic() - self.at_s

    def fresh(self, within_s: float = 1.0) -> tuple[Obstacle, ...] | None:
        return self.obstacles if self.age_s() <= within_s else None

    def look(self) -> None:
        ahead, left = into(self._world.pose(), self._world.things).T
        from_lens = np.column_stack([ahead, left]) - self._setup.lens_xy
        aside = np.abs(np.degrees(np.arctan2(from_lens[:, 1], from_lens[:, 0])))
        seen = (aside < self._setup.camera.hfov_deg / 2 - 3) & (from_lens[:, 0] > 0.45)
        seen &= np.hypot(*from_lens.T) < 3.5
        self.obstacles = tuple(
            Obstacle(f"s{n}", "object", float(a), float(b), radius_m=0.09)
            for n, (a, b) in enumerate(zip(ahead[seen], left[seen], strict=True))
        )
        self.at_s = time.monotonic() - 0.1


class KnowingPilot:
    """Stands in for route_pilot.RoutePilot: it knows the way and draws it on the picture."""

    model, thinking = "stand-in", "none"
    prompt_file = Path(__file__)

    def __init__(self, course: dict[str, Any], world: World, setup: chair_setup.ChairSetup) -> None:
        self.task = "follow the course"
        self._way = np.array(course["way"], dtype=np.float64)
        self._face = course["face"]
        self._world, self._setup = world, setup
        self._noise = random.Random(0)
        self.last: dict[str, Any] = {}

    def next(self, picture: bytes, report: str) -> Plan:
        pose = self._world.pose()  # where the picture was taken
        time.sleep(ANSWER_S)
        self.last = {"prompt": report, "raw": "stand-in", "ms": round(ANSWER_S * 1000)}
        # The way from here on: every half metre of it, from the nearest point onward.
        legs = np.vstack([[pose[:2]], self._way])
        fine = np.vstack(
            [
                np.linspace(a, b, max(2, round(np.linalg.norm(b - a) / 0.1)))
                for a, b in zip(self._way[:-1], self._way[1:], strict=True)
            ]
        )
        nearest = int(np.argmin(np.linalg.norm(fine - pose[:2], axis=1)))
        ahead = fine[nearest + 6 :: 5]
        end = into(pose, [self._way[-1]])[0]
        if math.hypot(*end) < 0.35 or len(ahead) == 0:
            return Plan("arrived", note="at the end of the way")
        del legs
        chair_frame = into(pose, np.vstack([ahead, [self._way[-1]]]))
        chair_frame = chair_frame[np.hypot(*chair_frame.T) < 3.0][:6]
        pixels = self._setup.to_pixels(chair_frame, 1000, 1000)
        inside = ~np.isnan(pixels).any(axis=1) & (pixels[:, 0] > 20) & (pixels[:, 0] < 980)
        inside &= pixels[:, 1] < 990
        if inside.sum() < 2 or not inside[0]:
            # The way is out of the picture: turn toward it.
            bearing = math.atan2(chair_frame[0][1], chair_frame[0][0]) if len(chair_frame) else 0.0
            return Plan("look_left" if bearing > 0 else "look_right", note="the way is to one side")
        points = tuple(
            (
                x / 1000 + self._noise.gauss(0, POINT_NOISE),
                y / 1000 + self._noise.gauss(0, POINT_NOISE),
            )
            for x, y in pixels[: int(np.argmin(inside)) if not inside.all() else len(pixels)]
        )
        return Plan("route", points=points, note="following the way")


def run(name: str, work: Path) -> dict[str, Any]:
    course = COURSES[name]
    setup = chair_setup.load()
    world = World(np.vstack(course["things"]))
    session = Session(FanDriver(), work, "fan")
    session.connect_live("http://127.0.0.1:9")  # nothing listens there: frames are fed by hand
    if session.sight is not None:
        session.sight.stop()
    session.sight, session._depth = Sight(world, setup), None
    session._sender = Sender(world)
    session.chair_listening = lambda wait_s=0.7: True
    session.page_alive = lambda: True
    full = session.setting("v_full_mps")
    # Two different pictures: sent turn about while the chair moves, so the picture changes
    # as a camera's would, and the same one while it stands.
    header = json.dumps({"seq": 0, "age_ms": 40}).encode()
    frames = []
    for seed in (1, 2):
        noise = np.random.default_rng(seed).integers(0, 255, (27, 48, 3), dtype=np.uint8)
        picture = io.BytesIO()
        Image.fromarray(noise).resize((480, 270)).save(picture, "JPEG")
        frames.append(struct.pack("<I", len(header)) + header + picture.getvalue())
    stop = threading.Event()
    drift: list[float] = []

    def phone() -> None:
        last, n = time.monotonic(), 0
        while not stop.is_set():
            now = time.monotonic()
            world.step(now, now - last, setup.chair, full)
            last, n = now, n + 1
            if n % 5 == 0:
                motion = {"fresh": True, "yaw_rate_dps": 0.0, "yaw_deg": math.degrees(world.yaw)}
                session.live._take(json.dumps({"phone_connected": True, "motion": motion}))
                moving = world.stick[1] != 0 or abs(world.rate_dps) > 2
                session.live._take(frames[(n // 5) % 2 if moving else 0])
            if n % 10 == 0:
                session.sight.look()
            time.sleep(0.01)

    threading.Thread(target=phone, daemon=True).start()
    time.sleep(0.5)
    session._armed, session._page_at = True, time.monotonic()
    session.episode = session.runlog.episode(kind="route", task=name)
    started = time.monotonic()
    work_done = session.runner.route(name, KnowingPilot(course, world, setup), max_looks=40)
    del work_done
    while session.runner.running():
        time.sleep(0.2)
        if time.monotonic() - started > LIMIT_S:
            session.stop(f"the simulation's {LIMIT_S:.0f} s ran out")
        # The harness's believed place (it began at the same spot, facing the same way).
        believed = session._pose_xy
        drift.append(math.hypot(believed[0] - world.x, believed[1] - world.y))
    stop.set()
    state = session.runner.state()
    end = course["way"][-1]
    episode = sorted(session.runlog.folder.glob("episode-*.jsonl"))[-1]
    ends = [json.loads(line) for line in episode.read_text().splitlines()]
    stops = [e["ended"] for e in ends if e["type"] == "action_end" and e["ended"] != "finished"]
    return {
        "course": name,
        "outcome": state.get("outcome"),
        "reason": state.get("reason"),
        "seconds": round(time.monotonic() - started, 1),
        "looks": state.get("steps"),
        "ended_from_goal_m": round(math.hypot(world.x - end[0], world.y - end[1]), 2),
        "closest_m": round(world.closest_m, 2),
        "worst_drift_m": round(max(drift, default=0.0), 2),
        "moves_ended_early": stops,
        "why": [
            e["detail"]
            for e in ends
            if e["type"] == "action_end" and e["ended"] not in ("finished", "replaced")
        ],
        "episode": str(episode),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("courses", nargs="*", choices=list(COURSES), help="default: all")
    args = parser.parse_args()
    for name in args.courses or COURSES:
        with tempfile.TemporaryDirectory() as work:
            result = run(name, Path(work))
            result.pop("episode")
            print(json.dumps(result))


if __name__ == "__main__":
    main()
