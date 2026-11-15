"""Watch a model decide, frame by frame, on a video.

    uv run python -m fsd_ml.viewer path/to/video.mp4

Opens a page showing the video with every path the model proposes, the one the
footprint check chose, and the speed and turn rate that would go to the chair.

The video does not react to the decisions: this shows what the model wants at
each frame, not what would happen if the chair followed it. There is no
obstacle model yet, so the footprint check has nothing to reject.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import threading
import time
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import numpy as np
from PIL import Image

from fsd_ml import bench, chair, detect, obstacles
from fsd_ml.chair_out import (
    ChairOutError,
    ChairSender,
    to_stick,
)
from fsd_ml.drivers import Candidate, Driver, Goal, Motion, make_driver
from fsd_ml.episode import Limits, Runner
from fsd_ml.live import ChairGoal, LiveFeed, ipad_message
from fsd_ml.memory import ObstacleMemory, View
from fsd_ml.pilot import Pilot
from fsd_ml.pipeline.control import ControlConfig, follow
from fsd_ml.pipeline.footprint import ChairShape, FootprintConfig, choose
from fsd_ml.video import FPS, Conversion, VideoError, standardize

PAGE = Path(__file__).with_name("page.html")
BENCH_PAGE = Path(__file__).with_name("bench.html")
MAX_UPLOAD_BYTES = 2 * 1024**3
# How the goal is given: a point, an instruction, a photo, or one of those with a point.
GOAL_KINDS = ("point", "say", "photo", "say+point", "photo+point")
# In the live view only: take the goal the rider set on the iPad.
FROM_IPAD = "ipad"
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


class ViewerError(Exception):
    """Something the page should show to the person using it."""


class DrivingServiceGiven(ViewerError):
    """A driving service's address was given where a model's was expected."""

    def __init__(self, service: str) -> None:
        super().__init__("that address is the driving service, not a model")
        self.service = service


def sender_stopped() -> dict[str, Any]:
    """What the page is told when sending was switched off for it."""
    return {"sent": False, "v_mps": 0.0, "w_radps": 0.0, "x": 0, "y": 0, "halted": True}


def is_driving_service(url: str) -> bool:
    try:
        answer = httpx.get(f"{url.rstrip('/')}/api/status", timeout=5.0)
        return answer.status_code == 200 and "phone_connected" in answer.text
    except httpx.HTTPError:
        return False


MODEL_TITLES = {
    "omnivla": "OmniVLA",
    "fan": "Fan (a stand-in, not a model)",
    "straight": "Straight ahead (a stand-in, not a model)",
}


def describe(driver: Driver) -> dict[str, Any]:
    """Which model this is and where it is running, in words for the page."""
    remote = driver.name.startswith("remote-")
    name = driver.name.removeprefix("remote-")
    provider = getattr(driver, "provider", None)
    if provider is None:
        hardware = None
    elif "CPU" in provider:
        hardware = "CPU"
    elif "CUDA" in provider:
        hardware = "GPU"
    else:
        hardware = provider  # a GPU's own name
    place = getattr(driver, "host", "another machine") if remote else "this computer"
    return {
        "name": driver.name,
        "title": MODEL_TITLES.get(name, name),
        "remote": remote,
        "runs_on": f"{place} ({hardware})" if hardware else place,
        "context_frames": driver.context_frames,
        "frame_hz": driver.frame_hz,
    }


class Session:
    """One loaded video and the decisions made on it so far."""

    def __init__(self, driver: Driver, work_dir: Path, driver_id: str | None = None) -> None:
        self.driver = driver
        self.driver_id = driver_id or driver.name
        self.work_dir = work_dir
        # Models already loaded, so switching back to one is instant.
        self._loaded: dict[str, Driver] = {self.driver_id: driver}
        self._hosted_file = work_dir / "hosted.json"
        self._service_file = work_dir / "service.json"
        # One folder per live connection: a line per decision, and the frames behind them.
        self._log_dir: Path | None = None
        self._frame_saved_s = 0.0
        self._waiting_logged: str | None = None
        # The camera's place on the chair and the chair's size, from ml/chair.json.
        self.setup = chair.load()
        body = self.setup.chair
        self.footprint = FootprintConfig(
            shape=ChairShape(body.width_m, body.length_m, body.margin_m)
        )
        self.control = ControlConfig()
        # Indoors things are close: react to what is within two metres, stop half a metre short.
        self.avoiding = FootprintConfig(
            shape=self.footprint.shape, caution_m=0.15, slow_distance_m=2.0, stop_distance_m=0.5
        )
        # The third model: a depth model that maps what stands on the floor. None if not installed.
        self.sight: obstacles.ObstacleWatcher | None = None
        # What the chair was last told to do, for moving the obstacle map: speed, turn, when.
        self._told = (0.0, 0.0, 0.0)
        self._depth: obstacles.DepthObstacles | None = None
        # Where the locked target stands, in the chair's frame, and when that was worked out.
        self._target_xy: tuple[float, float, float] | None = None
        # How the last live decision was steered, for the page and the record.
        self._steer: dict[str, Any] = {}
        # Stop with this much floor between the chair's front and the target.
        self.arrive_gap_m = 0.6
        # How far the chair has turned since the viewer started, by the gyro (positive left).
        self._yaw_rad = 0.0
        self._yaw_at: float | None = None
        # Autonomous episodes: only ever switched on by a person on the page.
        self._armed = False
        self._armed_with = (40.0, 10.0, 200.0)  # speed %, brake %, brake ms
        self._last_answer: dict[str, Any] | None = None
        self.runner = Runner(self, work_dir / "episodes")
        # Driving by hand from the page's keyboard: the model's decisions are then not sent.
        self._manual = False
        self._blocked_looks = 0
        # The chair's own count of joystick reads, and when it last went up. A chair that is
        # switched off or asleep stops reading, and then nothing sent to it does anything.
        self._reads: tuple[int | None, float] = (None, 0.0)
        # The test bench: its settings are read afresh for every run, so a change needs no restart.
        self.bench = bench.Bench(work_dir / "bench")
        self._bench_run: str | None = None
        # Where the chair believes it is, from the place "start" was set: metres ahead and
        # left of it, by the gyro and the speed it was told to do.
        self._pose_xy = [0.0, 0.0]
        self._yaw_at_start = 0.0
        self.video_name: str | None = None
        self.conversion: Conversion | None = None
        self.live: LiveFeed | None = None
        self.live_photo: np.ndarray | None = None
        self._live_jpeg: bytes | None = None
        self._chair_goal: ChairGoal | None = None
        # Set while the live view's decisions are being sent to the chair.
        self.chair_out: ChairSender | None = None
        # Decisions still allowed to reach the chair; None is no limit.
        self.chair_steps: int | None = None
        # Scales speed and turn together before they go to the chair: same curve, slower.
        self.chair_scale = 1.0
        # The reverse pull on a stop: stick percent and milliseconds. Zero is a plain stop.
        self.chair_brake = (0.0, 0.0)
        # The second model: boxes whatever the instruction names. None when not installed.
        self.watcher: detect.Watcher | None = None
        self._target: str | None = None
        self._target_at = 0.0
        self._said: str | None = None
        self.frame_count = 0
        self.size = (0, 0)
        self._frames_dir = work_dir / "frames"
        self._cache: dict[tuple[int, float, float, float], dict[str, Any]] = {}
        # The model runs one call at a time.
        self._lock = threading.Lock()

    def hosted(self) -> list[str]:
        """Addresses of hosted models added from the page; remembered between runs."""
        try:
            return [str(url) for url in json.loads(self._hosted_file.read_text())]
        except (OSError, ValueError):
            return []

    def _label(self, driver_id: str, unloaded: str) -> str:
        """A loaded model's name and where it runs; before loading, the best guess."""
        driver = self._loaded.get(driver_id)
        if driver is None:
            return unloaded
        described = describe(driver)
        return f"{described['title']} · {described['runs_on']}"

    def models(self) -> dict[str, Any]:
        """What the page can switch to: the hosted models, and the one in use."""
        options = [
            {
                "id": f"remote:{url}",
                "label": self._label(f"remote:{url}", f"Hosted model · {urlparse(url).hostname}"),
            }
            for url in self.hosted()
        ]
        if self.driver_id not in {option["id"] for option in options}:
            label = self._label(self.driver_id, self.driver_id)
            options.insert(0, {"id": self.driver_id, "label": label})
        return {"current": self.driver_id, "options": options}

    def switch(self, driver_id: str) -> None:
        """Use another model. A hosted one is given as ``remote:<address>`` and is remembered."""
        url = driver_id.removeprefix("remote:") if driver_id.startswith("remote:") else None
        if url is not None:
            parts = urlparse(url)
            local = parts.hostname in ("127.0.0.1", "localhost")
            if parts.scheme != "https" and not (parts.scheme == "http" and local):
                raise ViewerError("a hosted model's address must start with https://")
            driver_id = f"remote:{url.rstrip('/')}"
        elif driver_id not in self._loaded:
            raise ViewerError(f"no model called {driver_id}")

        with self._lock:
            if driver_id not in self._loaded:
                try:
                    self._loaded[driver_id] = make_driver(driver_id)
                except (httpx.HTTPError, KeyError, ValueError) as error:
                    if url is not None and is_driving_service(url):
                        raise DrivingServiceGiven(url.rstrip("/")) from error
                    if isinstance(error, httpx.HTTPStatusError):
                        code = error.response.status_code
                        reason = f"that address answered, but is not a model server ({code})"
                    else:
                        reason = f"could not reach that address: {error}"
                    raise ViewerError(reason) from error
            self.driver, self.driver_id = self._loaded[driver_id], driver_id
            self._cache.clear()
            (self.work_dir / "model.json").write_text(json.dumps(driver_id))
            if url is not None and url.rstrip("/") not in self.hosted():
                self._hosted_file.write_text(json.dumps([*self.hosted(), url.rstrip("/")]))

    def open(self, video: Path) -> None:
        with self._lock:
            try:
                self.conversion = standardize(video, self._frames_dir)
            except VideoError as error:
                raise ViewerError(str(error)) from error
            self.frame_count = self.conversion.frames
            self.video_name = video.name
            with Image.open(self.frame_path(0)) as first:
                self.size = first.size
            self._cache.clear()

    def frame_path(self, index: int) -> Path:
        return self._frames_dir / f"{index + 1:06d}.jpg"

    def info(self) -> dict[str, Any]:
        shape = self.footprint.shape
        return {
            "video": self.video_name,
            "live_service": self.last_service(),
            # What the upload was, and how it was put into the models' form.
            "conversion": self.conversion.as_dict() if self.conversion else None,
            "frames": self.frame_count,
            "fps": FPS,
            "width": self.size[0],
            "height": self.size[1],
            "driver": describe(self.driver),
            # Frames between one decision and the next.
            "step": max(1, round(FPS / getattr(self.driver, "decision_hz", self.driver.frame_hz))),
            "chair": {
                "width_m": shape.width_m,
                "length_m": shape.length_m,
                "margin_m": shape.margin_m,
            },
            "setup": self.setup.as_dict(),
        }

    def _window(self, index: int) -> list[np.ndarray]:
        """The frames the model sees at ``index``: its context, at its own frame rate."""
        step = max(1, round(FPS / self.driver.frame_hz))
        indices = [max(0, index - step * back) for back in range(self.driver.context_frames)]
        frames = []
        for i in reversed(indices):
            with Image.open(self.frame_path(i)) as image:
                frames.append(np.asarray(image.convert("RGB")))
        return frames

    def _goal(
        self,
        kind: str,
        goal_xy: tuple[float, float],
        instruction: str,
        photo: np.ndarray | None,
    ) -> Goal:
        """The goal for one of GOAL_KINDS. Models that only read points ignore the rest."""
        if kind == FROM_IPAD:
            raise ViewerError("the iPad's goal is only available in the live view")
        if kind not in GOAL_KINDS:
            raise ViewerError(f"goal type must be one of {', '.join(GOAL_KINDS)}")
        point, wants = "point" in kind, kind.split("+")[0]
        if wants == "say" and not instruction.strip():
            raise ViewerError("type an instruction first")
        if wants == "photo" and photo is None:
            raise ViewerError("choose a frame as the goal photo first")
        return Goal(
            point_xy=goal_xy if point else None,
            instruction=instruction.strip() if wants == "say" else None,
            image=photo if wants == "photo" else None,
        )

    def _around(
        self, v: float, w: float, speed_mps: float
    ) -> tuple[str, str | None, float, float, np.ndarray | None, list[list[float]] | None]:
        """Take the model's heading, and go round whatever stands in its way.

        Returns the status, the reason, the speed and turn rate to use, the way chosen and
        the obstacles it was chosen against. With no fresh sight of the floor, the chair stops.
        """
        seen = self.sight.fresh() if self.sight else None
        if seen is None or not self.sight.info.get("floor"):
            why = "Cannot see the floor ahead" if seen is not None else "Obstacle view is stale"
            return "blocked", why, 0.0, 0.0, None, None
        listed = self.sight.listed
        length = obstacles.AROUND_M
        target = self._locate_target()
        self._steer = {"steer": "model", "target_xy": None}
        if target is not None:
            # The target's place is known: head for it, whatever the driving model drew.
            ahead, left = target
            away = math.hypot(ahead, left)
            gap = away - self.setup.chair.length_m / 2
            self._steer = {
                "steer": "target",
                "target_xy": [round(ahead, 2), round(left, 2)],
                "target_gap_m": round(gap, 2),
                "model_vw": [round(v, 3), round(w, 3)],
            }
            if gap <= self.arrive_gap_m:
                return "arrived", "Arrived at the target", 0.0, 0.0, None, listed
            v, w = obstacles.toward(ahead, left, 0.9)
            # The target is itself a thing on the floor: it is not in its own way.
            seen = [o for o in seen if math.hypot(o.x - ahead, o.y - left) > 0.45]
            length = max(0.4, min(length, away - self.setup.chair.length_m / 2))
        if v < 0.05:
            return "clear", None, v, w, None, listed  # turning on the spot, or not moving
        ways = obstacles.ways_round(v, w, length)
        picked = choose([Candidate(path, score) for _, path, score in ways], seen, self.avoiding)
        if picked.index is None or picked.status == "blocked":
            return "blocked", picked.reason or "No clear way", 0.0, 0.0, picked.path_xy, listed
        v2, w2 = follow(picked.path_xy, picked.speed_factor, speed_mps, 1.0 / FPS, self.control, v)
        return picked.status, picked.reason, v2, w2, picked.path_xy, listed

    def _decision(
        self, frames: list[np.ndarray], goal: Goal, speed_mps: float, live: bool = False
    ) -> dict[str, Any]:
        """Ask the model, then choose a path and a command as the pipeline would."""
        started = time.perf_counter()
        candidates = self.driver.propose(frames, goal, Motion(speed_mps, 0.0))
        inference_ms = (time.perf_counter() - started) * 1000.0
        # The model draws from the lens; everything after this is about the chair.
        candidates = [
            Candidate(self.setup.to_chair(c.path_xy), c.score, c.speed_mps) for c in candidates
        ]

        choice = choose(candidates, [], self.footprint)
        # Steer by the path as the model drew it: shifting it sideways would pull the
        # chair toward the camera's side for ever. The shift is for the fit check only.
        v, w = follow(
            choice.path_xy - self.setup.lens_xy if len(choice.path_xy) else choice.path_xy,
            choice.speed_factor,
            speed_mps,
            1.0 / FPS,
            self.control,
            choice.speed_limit_mps,
        )
        status, reason, route, seen_obstacles = choice.status, choice.reason, None, None
        if live and self.sight is not None:
            status, reason, v, w, route, seen_obstacles = self._around(v, w, speed_mps)
        return {
            "inference_ms": round(inference_ms, 1),
            "candidates": [
                {
                    "score": round(float(c.score), 4),
                    "speed_mps": None if c.speed_mps is None else round(c.speed_mps, 2),
                    "path": np.round(c.path_xy, 3).tolist(),
                }
                for c in candidates
            ],
            "chosen": choice.index,
            "status": status,
            "reason": reason,
            # The way the chair will actually go, when obstacles are being watched for.
            "route": None if route is None else np.round(route, 3).tolist(),
            "obstacles": seen_obstacles,
            "sight": self.sight.info if live and self.sight is not None else None,
            **(self._steer if live else {}),
            "v_mps": round(v, 3),
            "w_radps": round(w, 3),
        }

    # Live: the chair's phone, by way of the driving service.

    def connect_live(self, service: str) -> None:
        try:
            feed = LiveFeed(service)
        except ValueError as error:
            raise ViewerError(str(error)) from error
        self.disconnect_live()
        self.live, self.live_photo = feed, None
        self._chair_goal = ChairGoal(service)
        feed.start()
        self._service_file.write_text(json.dumps(service))
        self._log_dir = self.work_dir / "live" / time.strftime("%Y%m%d-%H%M%S")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self.log("connected", service=service, model=self.driver.name)
        self._target = None
        if detect.available():
            self.watcher = detect.Watcher(self._look, measure=self._box_distance)
            self.watcher.start()
            depth, camera = obstacles.DepthObstacles(), self.setup.camera
            self._depth = depth
            body = self.setup.chair
            memory = ObstacleMemory(
                View(self.setup.lens_xy, camera.hfov_deg),
                # The drive wheels' axle: about a wheel's radius in from the back of the chair.
                pivot_x=-(body.length_m / 2 - 0.15),
                half_length_m=body.length_m / 2,
                half_width_m=body.width_m / 2,
                margin_m=body.margin_m,
            )
            self.sight = obstacles.ObstacleWatcher(
                lambda: self._look()[0],
                lambda image: depth.find(
                    image, camera.hfov_deg, camera.height_m, self.setup.lens_xy
                ),
                memory,
                self._moving,
            )
            self.sight.start()

    def _moving(self) -> tuple[float, float]:
        """How the chair is moving now, as well as can be known: (m/s, rad/s positive left).

        Forward speed is what the chair was last told, while that command is still live;
        nothing measures it. Turning comes from the phone's gyro when that is fresh.
        """
        speed, turn, at = self._told
        if time.monotonic() - at > 0.7:  # the command has run out: the chair has stopped
            speed = turn = 0.0
        sample = self.live.latest() if self.live else None
        motion = (sample.status.get("motion") if sample else None) or {}
        rate = motion.get("yaw_rate_dps")
        if motion.get("fresh") and rate is not None:
            # A resting gyro wanders a little; that is not the chair turning.
            turn = math.radians(rate) if abs(rate) > 0.5 else 0.0
        now = time.monotonic()
        if self._yaw_at is not None and now - self._yaw_at < 1.0:
            dt = now - self._yaw_at
            facing = self._yaw_rad - self._yaw_at_start + turn * dt / 2
            self._pose_xy[0] += speed * dt * math.cos(facing)
            self._pose_xy[1] += speed * dt * math.sin(facing)
            self._yaw_rad += turn * dt
        self._yaw_at = now
        return speed, turn

    @property
    def v_full_mps(self) -> float:
        return self.bench.settings()["v_full_mps"]

    @property
    def w_full_radps(self) -> float:
        return self.bench.settings()["w_full_radps"]

    def setting(self, name: str) -> float:
        return self.bench.settings()[name]

    def pose(self) -> tuple[float, float, float]:
        return self._pose_xy[0], self._pose_xy[1], self._yaw_rad - self._yaw_at_start

    def set_start(self) -> dict[str, Any]:
        """Call where the chair is now "the start": the place a return goes back to."""
        self._pose_xy = [0.0, 0.0]
        self._yaw_at_start = self._yaw_rad
        self.log("start set")
        return self.bench_state()

    def finished(self, record: dict[str, Any]) -> None:
        """A run is over: write it in the journal, and take the bench's permission away."""
        if self._bench_run is not None:
            ahead, left, yaw = self.pose()
            self.bench.write(
                "result",
                run=self._bench_run,
                outcome=record.get("outcome"),
                reason=record.get("reason"),
                seconds=record.get("seconds"),
                ended=[round(ahead, 2), round(left, 2), round(math.degrees(yaw))],
            )
            self._bench_run = None
            self._armed = False

    def bench_run(self, case_id: str, say: str | None = None) -> dict[str, Any]:
        """Start one of the bench's tests. The press of Run on the page is the permission."""
        chosen = bench.case(case_id)
        if chosen is None and case_id != "home":
            raise ViewerError(f"no test called {case_id}")
        if self.runner.running():
            raise ViewerError("a run is already going; stop it first")
        if self.live is None:
            raise ViewerError("not connected to the chair's driving service")
        if self._manual:
            self.manual(False)
        params = dict(chosen["params"]) if chosen else {}
        if chosen and chosen.get("input") == "say" and say and say.strip():
            params["say"] = say.strip()
        self._armed = True
        self._bench_run = self.bench.new_run(case_id, params)
        kind = chosen["kind"] if chosen else "home"
        try:
            if kind == "straight":
                self.runner.straight(params["distance_m"])
            elif kind == "turn":
                self.runner.turn(params["degrees_left"])
            elif kind == "square":
                self.runner.square(params["side_m"], params["left"])
            elif kind == "circle":
                self.runner.circle(params["diameter_m"], params["left"])
            elif kind == "go":
                self.arrive_gap_m = self.setting("arrive_gap_m")
                self.runner.go(params["say"])
            elif kind == "pilot":
                key = detect.claude_key()
                if not key:
                    raise RuntimeError("no Claude API key: put ANTHROPIC_API_KEY in ml/.env")
                self._target_at = 0.0  # the pilot looks for itself; the detector rests
                self.runner.pilot(
                    params["say"],
                    Pilot(params["say"], api_key=key),
                    max_turns=max(1, round(self.setting("pilot_max_looks"))),
                )
            else:
                self.runner.home()
        except RuntimeError as error:
            self._armed, self._bench_run = False, None
            raise ViewerError(str(error)) from error
        return self.bench_state()

    def bench_feedback(self, run: str, rating: str, measured: str, text: str) -> dict[str, Any]:
        self.bench.write(
            "feedback", run=run, rating=rating[:20], measured=measured[:80], text=text[:2000]
        )
        return self.bench_state()

    def bench_state(self) -> dict[str, Any]:
        """Everything the bench page shows that is not the camera."""
        ahead, left, yaw = self.pose()
        board = self.board() if self.chair_out is not None else {}
        return {
            "cases": bench.CASES,
            "latest": self.bench.latest(),
            "notes": self.bench.notes(),
            "settings": self.bench.settings(),
            "episode": self.runner.state(),
            "pose": {
                "ahead_m": round(ahead, 2),
                "left_m": round(left, 2),
                "turned_deg": round(math.degrees(yaw)),
            },
            "manual": self._manual,
            "connected": self.live is not None,
            "model": self.driver.name,
            "board": board,
        }

    def bench_view(self) -> dict[str, Any]:
        """The live picture and what is seen in it, without asking the driving model anything."""
        if self.live is None:
            return {"waiting": "not connected to the chair's driving service"}
        sample = self.live.latest()
        status = sample.status
        motion = status.get("motion") or {}
        problem = self.live.problem
        if problem is None and not status.get("phone_connected"):
            problem = "no phone is streaming"
        if problem is None and sample.frame is None:
            problem = "waiting for the first camera frame"
        seq = None
        if sample.frame is not None and not self.runner.running():
            # While a run that asks the model is going, its own frames are the ones shown.
            buffer = io.BytesIO()
            Image.fromarray(sample.frame.image).save(buffer, format="JPEG", quality=80)
            self._live_jpeg, seq = buffer.getvalue(), sample.frame.n
        elif sample.frame is not None:
            seq = sample.frame.n
            if self._live_jpeg is None or self.runner.state().get("kind") != "go":
                buffer = io.BytesIO()
                Image.fromarray(sample.frame.image).save(buffer, format="JPEG", quality=80)
                self._live_jpeg = buffer.getvalue()
        return {
            "waiting": problem,
            "seq": seq,
            "gyro_fresh": bool(motion.get("fresh")),
            "sight": self.sight.info if self.sight else None,
            "obstacles": self.sight.listed if self.sight else [],
            "seen": self.watcher.latest() if self.watcher else None,
            "route": (self._last_answer or {}).get("route") if self.runner.running() else None,
            "target_xy": self._steer.get("target_xy") if self.runner.running() else None,
            "chair": self.info()["chair"],
            "lens": [self.setup.lens_xy[0], self.setup.lens_xy[1]],
        }

    # What the episode runner needs (fsd_ml/episode.py).

    def armed(self) -> bool:
        return self._armed

    def arm(
        self, on: bool, speed_pct: float = 40.0, brake_pct: float = 10.0, brake_ms: float = 200.0
    ):
        """Allow, or stop allowing, the chair to be driven without a person ticking each run."""
        if on and self.live is None:
            raise ViewerError("connect to the driving service first")
        self._armed = on
        self._armed_with = (speed_pct, brake_pct, brake_ms)
        if not on:
            self.runner.abort("disarmed from the page")
        self.log("armed" if on else "disarmed", speed_pct=speed_pct)
        return {"armed": on}

    def begin(self) -> str | None:
        settings = self.bench.settings()
        try:
            if self._bench_run is not None:
                self.send_to_chair(
                    True,
                    None,
                    settings["goto_speed_pct"],
                    settings["brake_pct"],
                    settings["brake_ms"],
                )
            else:
                self.send_to_chair(True, None, *self._armed_with)
        except ViewerError as error:
            return str(error)
        if not self.chair_listening():
            return "the chair is not reading its joystick: is it switched on?"
        # A push on the real joystick earlier leaves the board saying "taken over" until it
        # has heard that driving is switched on again. Give it a moment to say so.
        until = time.monotonic() + 2.0
        while self.board().get("takeover") and time.monotonic() < until:
            time.sleep(0.1)
        if self.board().get("takeover"):
            return "the chair's own joystick is being held or is off-centre: let go of it"
        return None

    def end(self) -> None:
        self.send_to_chair(False)

    def step(self, say: str) -> dict[str, Any]:
        return self.live_decide((5.0, 0.0), 0.0, "say", say, from_episode=True)

    def hold(self) -> None:
        if self.chair_out is not None:
            self.chair_out.brake(*self.chair_brake)
            self._told = (0.0, 0.0, time.monotonic())

    def turn(self, stick_pct: float) -> str | None:
        sender = self.chair_out
        if sender is None:
            return "sending was switched off"
        result = sender.send_stick(stick_pct, 0.0)
        if result.halted:
            self._armed = False
            return result.problem
        self._told = (0.0, -result.x / 100 * self.w_full_radps, time.monotonic())
        return None

    def yaw_rad(self) -> float:
        return self._yaw_rad

    def board(self) -> dict[str, Any]:
        """The board's telemetry, with whether the chair is listening to its joystick at all."""
        sender = self.chair_out
        if sender is None:
            return {}
        stats, now = sender.telemetry(), time.monotonic()
        reads = stats.get("reads")
        if reads != self._reads[0]:
            self._reads = (reads, now)
        # No reads for a while: the chair is off or asleep. Unknown (no count) is not asleep.
        stats["chair_listening"] = reads is None or now - self._reads[1] < 1.5
        return stats

    def chair_listening(self, wait_s: float = 0.7) -> bool:
        """Watch the read count for a moment: does the chair take any notice of the board?"""
        self.board()
        time.sleep(wait_s)
        return bool(self.board().get("chair_listening", True))

    def drive(self, x_pct: float, y_pct: float, reverse: bool = False) -> str | None:
        """Send a raw stick position (x right, y forward). A reason if it was not sent."""
        sender = self.chair_out
        if sender is None:
            return "sending was switched off"
        result = sender.send_stick(x_pct, y_pct, reverse)
        if result.halted:
            self._armed = self._manual = False
            self.send_to_chair(False)
            return result.problem
        self._told = (
            result.y / 100 * self.v_full_mps,
            -result.x / 100 * self.w_full_radps,
            time.monotonic(),
        )
        return None

    def observe(self) -> tuple[bytes | None, str]:
        """The camera's newest picture, and what the chair itself knows, in a few plain lines."""
        sample = self.live.latest() if self.live else None
        if sample is None or sample.frame is None:
            return None, ""
        buffer = io.BytesIO()
        Image.fromarray(sample.frame.image).save(buffer, format="JPEG", quality=90)
        jpeg, _ = detect.original_or_copy(sample.frame.jpeg, sample.frame.image)
        self._live_jpeg = buffer.getvalue()
        ahead, left, yaw = self.pose()
        lines = [
            f"The chair believes it is {ahead:+.1f} m ahead and {left:+.1f} m left of where it"
            f" started, and has turned {math.degrees(yaw):+.0f} degrees (left is positive)."
        ]
        seen = self.sight.fresh() if self.sight else None
        if self.sight is None:
            lines.append("The chair has no obstacle sense running: go by the picture alone.")
        elif seen is None or not self.sight.info.get("floor"):
            lines.append("The chair's obstacle sense cannot see the floor right now.")
        else:
            body = self.setup.chair
            front, half = body.length_m / 2, body.width_m / 2

            def nearest(inside: Any) -> float | None:
                found = [o.x - front for o in seen if o.x > front - 0.1 and inside(o)]
                return min(found) if found else None

            def said(gap: float | None, where: str) -> str:
                if gap is None:
                    return f"{where}: nothing sensed within 3 m."
                return f"{where}: something about {max(0.0, gap):.1f} m from the chair's front."

            lines.append("The chair's obstacle sense (rough, and blind closer than 0.7 m):")
            lines.append("- " + said(nearest(lambda o: abs(o.y) <= half + 0.15), "Straight ahead"))
            lines.append(
                "- " + said(nearest(lambda o: half + 0.15 < o.y <= half + 0.9), "Ahead to the left")
            )
            lines.append(
                "- "
                + said(nearest(lambda o: -half - 0.9 <= o.y < -half - 0.15), "Ahead to the right")
            )
        return jpeg, "\n".join(lines)

    def note(self, event: str, **fields: Any) -> None:
        self.log(event, episode=self.runner.state().get("id"), **fields)

    def speed_for(self, y_pct: float) -> float:
        return max(0.0, y_pct) / 100 * self.v_full_mps

    def calibrate(self, v_full_mps: float | None, w_full_radps: float | None) -> dict[str, float]:
        """Record what full stick really gives, from a measured run."""
        changes = {}
        if v_full_mps is not None:
            changes["v_full_mps"] = max(0.05, min(5.0, v_full_mps))
        if w_full_radps is not None:
            changes["w_full_radps"] = max(0.05, min(6.0, w_full_radps))
        saved = self.bench.set(**changes)
        self.log("calibrated", **changes)
        return saved

    def blocked(self, v_mps: float, w_radps: float) -> str | None:
        """Whether the next metre of the way the chair is being driven meets an obstacle."""
        if self.sight is None:
            return None
        seen = self.sight.fresh()
        if seen is None or not self.sight.info.get("floor"):
            return "cannot see the floor ahead"
        # Only what the camera sees this moment. A set move turns the chair hard, and the
        # remembered map is not yet good enough through a turn to stop the chair on its word.
        seen = [o for o in seen if o.id.startswith("s")]
        bend = w_radps / v_mps if v_mps > 0.05 else 0.0
        way = obstacles.arc(max(-obstacles.MAX_BEND, min(obstacles.MAX_BEND, bend)), 1.0)
        # A set move is short and watched, so it keeps a hand's width clear, not the full
        # clearance used when finding a way across a room.
        body = self.setup.chair
        tight = FootprintConfig(
            shape=ChairShape(body.width_m, body.length_m, self.setting("clearance_m")),
            caution_m=0.05,
            slow_distance_m=2.0,
            stop_distance_m=0.4,
        )
        picked = choose([Candidate(way, 1.0)], seen, tight)
        if picked.status != "blocked":
            self._blocked_looks = 0
            return None
        # One look can be wrong, most of all while the chair pitches as it sets off. Three in
        # a row, about a third of a second, is something really there.
        self._blocked_looks += 1
        if self._blocked_looks < 3:
            return None
        near = min(seen, key=lambda o: math.hypot(o.x, o.y), default=None)
        where = f" ({near.x:.1f} m ahead, {abs(near.y):.1f} m {'left' if near.y > 0 else 'right'})"
        return (picked.reason or "obstacle") + (where if near else "")

    def manual(self, on: bool) -> dict[str, Any]:
        """Switch driving by hand from the page's keyboard on or off."""
        if on:
            if self.runner.running():
                raise ViewerError("an episode is driving; stop it first")
            if self.live is None:
                raise ViewerError("connect to the driving service first")
            if self.chair_out is None:
                try:
                    self.chair_out = ChairSender()
                except ChairOutError as error:
                    raise ViewerError(str(error)) from error
            self._manual = True
            self.chair_steps = None
            self.log("manual on")
            return {"manual": True}
        was, self._manual = self._manual, False
        if was:
            self.send_to_chair(False)
            self.log("manual off")
        return {"manual": False}

    def target(self) -> dict[str, Any]:
        seen = self.watcher.latest() if self.watcher else {}
        return {"locked": bool(seen.get("locked")), "note": seen.get("note")}

    def _locate_target(self) -> tuple[float, float] | None:
        """Where the locked target stands on the floor, from the newest depth map."""
        seen = self.watcher.latest() if self.watcher else {}
        depth = self._depth.last_depth if self._depth else None
        now = time.monotonic()
        if seen.get("locked") and seen.get("boxes") and depth is not None:
            box, camera = seen["boxes"][0], self.setup.camera
            found = obstacles.locate_box(
                depth,
                camera.hfov_deg,
                camera.height_m,
                (box["x0"], box["y0"], box["x1"], box["y1"]),
                self.setup.lens_xy,
            )
            if found is not None:
                self._target_xy = (*found, now)
        if self._target_xy is None or now - self._target_xy[2] > 2.0:
            return None
        return self._target_xy[0], self._target_xy[1]

    def _look(self) -> tuple[np.ndarray | None, str | None, str | None, bytes | None]:
        """The newest live frame and what to look for in it, for the detector."""
        sample = self.live.latest() if self.live else None
        frame = sample.frame.image if sample and sample.frame else None
        jpeg = sample.frame.jpeg if sample and sample.frame else None
        # An instruction typed on the page wins while that page is asking with it; otherwise
        # the iPad's instruction is the target the moment it arrives, whatever the page is set to.
        if time.monotonic() - self._target_at < 5.0:
            return frame, self._target, self._said, jpeg
        goal = (sample.status.get("goal") if sample else None) or {}
        said = goal.get("instruction") if goal.get("kind") == "instruction" else None
        return frame, detect.target_phrase(said), said, jpeg

    def _box_distance(self, box: detect.Box, aspect: float) -> float | None:
        camera = self.setup.camera
        distance = detect.floor_distance(
            box.y1, camera.height_m, camera.tilt_down_deg, camera.hfov_deg, aspect
        )
        return None if distance is None else round(distance, 2)

    def log(self, event: str, **fields: Any) -> None:
        """Append one line to this connection's record. Never gets in the way of driving."""
        if self._log_dir is None:
            return
        try:
            with (self._log_dir / "log.jsonl").open("a") as lines:
                lines.write(json.dumps({"t": round(time.time(), 3), "event": event, **fields}))
                lines.write("\n")
        except (OSError, TypeError, ValueError):
            pass

    def _log_waiting(self, why: str | None) -> None:
        """Record why nothing was decided, once each time the reason changes."""
        if why != self._waiting_logged:
            self._waiting_logged = why
            self.log("waiting", why=why)

    def _log_decision(self, answer: dict[str, Any], kind: str, instruction: str) -> None:
        """Record a decision and the frame it was made on."""
        chair, frame = answer.get("chair"), None
        now = time.monotonic()
        if self._log_dir and self._live_jpeg:
            frame = f"{answer['seq']:07d}.jpg"
            try:
                (self._log_dir / frame).write_bytes(self._live_jpeg)
                self._frame_saved_s = now
            except OSError:
                frame = None
        chosen = answer.get("chosen")
        candidates = answer.get("candidates") or []
        self._waiting_logged = None
        self.log(
            "decision",
            seq=answer["seq"],
            frame=frame,
            goal_kind=kind,
            say=instruction or None,
            goal_xy=answer.get("goal_xy"),
            goal_note=answer.get("goal_note"),
            ipad=(answer.get("ipad") or {}).get("text"),
            model=self.driver.name,
            sensors=answer.get("sensors"),
            candidates=candidates,
            chosen=chosen,
            lens_xy=list(self.setup.lens_xy),
            inference_ms=answer.get("inference_ms"),
            round_trip_ms=answer.get("round_trip_ms"),
            seen=answer.get("seen"),
            route=answer.get("route"),
            steer=answer.get("steer"),
            target_xy=answer.get("target_xy"),
            target_gap_m=answer.get("target_gap_m"),
            model_vw=answer.get("model_vw"),
            episode=self.runner.state().get("id") if self.runner.running() else None,
            obstacles=answer.get("obstacles"),
            sight=answer.get("sight"),
            frame_age_ms=answer["link"].get("frame_age_ms"),
            status=answer.get("status"),
            reason=answer.get("reason"),
            path=candidates[chosen]["path"] if chosen is not None and candidates else None,
            v_mps=answer.get("v_mps"),
            w_radps=answer.get("w_radps"),
            chair=chair,
        )

    def last_service(self) -> str | None:
        """The driving service last connected to, remembered between runs."""
        try:
            return str(json.loads(self._service_file.read_text()))
        except (OSError, ValueError):
            return None

    def disconnect_live(self) -> None:
        self.send_to_chair(False)
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        if self.sight is not None:
            self.sight.stop()
            self.sight = None
        if self.live is not None:
            self.live.stop()
        self.live = self._live_jpeg = None

    def send_to_chair(
        self,
        on: bool,
        steps: int | None = None,
        speed_pct: float = 100.0,
        brake_pct: float = 0.0,
        brake_ms: float = 0.0,
    ) -> dict[str, Any]:
        """Start or stop sending live decisions to the chair (hardware/wheelchair.py).

        With ``steps``, only that many decisions are sent, then the chair is stopped.
        Stopping never waits on the model, so it works as the emergency stop.
        """
        if not on:
            sender, self.chair_out = self.chair_out, None
            self.chair_steps = None
            self._manual = False
            self._told = (0.0, 0.0, time.monotonic())
            if sender is not None:
                self.log("sending off")
            if sender is None:
                return {"sending": False}
            stopped = sender.brake(*self.chair_brake)
            self.log("stop", braked_y=stopped.y, board=sender.telemetry())
            sender.close()
            return {"sending": False, "chair": stopped.as_dict()}
        if self.live is None:
            raise ViewerError("connect to the driving service first")
        if not getattr(self.driver, "remote", False) and self.driver.name in ("fan", "straight"):
            raise ViewerError(
                f"{self.driver.name} is a stand-in, not a model: pick the real model first"
            )
        if self.chair_out is None:
            try:
                self.chair_out = ChairSender()
            except ChairOutError as error:
                raise ViewerError(str(error)) from error
        self.chair_steps = steps if steps and steps > 0 else None
        self.chair_scale = max(0.1, min(1.0, speed_pct / 100.0))
        self.chair_brake = (max(0.0, brake_pct), max(0.0, brake_ms))
        self.log(
            "sending on",
            steps=self.chair_steps,
            speed_pct=round(self.chair_scale * 100),
            brake_pct=self.chair_brake[0],
            brake_ms=self.chair_brake[1],
        )
        return {"sending": True, "steps_left": self.chair_steps}

    def chair_settings(self, speed_pct: float, brake_pct: float, brake_ms: float) -> dict[str, Any]:
        """Change speed and braking while sending, without restarting the step count."""
        self.chair_scale = max(0.1, min(1.0, speed_pct / 100.0))
        self.chair_brake = (max(0.0, brake_pct), max(0.0, brake_ms))
        self.log(
            "settings",
            speed_pct=round(self.chair_scale * 100),
            brake_pct=self.chair_brake[0],
            brake_ms=self.chair_brake[1],
        )
        return {"speed_pct": round(self.chair_scale * 100)}

    def _to_chair(self, decision: dict[str, Any] | None) -> dict[str, Any] | None:
        """Send a decision, or stop when there is none, if sending is on. For the page."""
        sender = self.chair_out
        if sender is None:
            return None
        if self._manual:
            # A person is driving from the keyboard: the model is watched, not obeyed.
            return {"sent": False, "manual": True, "board": self.board()}
        if decision is None:
            self._told = (0.0, 0.0, time.monotonic())
            return {**sender.brake(*self.chair_brake).as_dict(), "board": self.board()}
        if self.chair_steps is not None and self.chair_steps <= 0:
            self.send_to_chair(False)
            return {**sender_stopped(), "problem": "the steps asked for are done"}
        # Scaled after the stick cap, so the setting slows the chair even when the cap binds.
        v, w, scale = decision["v_mps"], decision["w_radps"], self.chair_scale
        x, y = to_stick(v, w)
        result = replace(sender.send_stick(x * scale, y * scale), v_mps=v, w_radps=w)
        if result.sent:
            # What the stick that was really sent should make the chair do.
            self._told = (
                result.y / 100 * self.v_full_mps,
                -result.x / 100 * self.w_full_radps,
                time.monotonic(),
            )
        if result.halted:  # the board took control back: the rider must tick again
            self.send_to_chair(False)
        elif result.sent and self.chair_steps is not None:
            self.chair_steps -= 1
        return {
            **result.as_dict(),
            "steps_left": self.chair_steps,
            "board": self.board(),
        }

    def keep_live_photo(self) -> None:
        """Use what the camera sees now as the goal photo."""
        sample = self.live.latest() if self.live else None
        if sample is None or sample.frame is None:
            raise ViewerError("no live frame yet")
        self.live_photo = sample.frame.image

    def live_now(self) -> bytes:
        """The newest picture exactly as the phone sent it, for the page's smooth camera layer."""
        sample = self.live.latest() if self.live else None
        if sample is None or sample.frame is None or not sample.frame.jpeg:
            raise ViewerError("no live frame yet")
        return sample.frame.jpeg

    def live_frame(self) -> bytes:
        if self._live_jpeg is None:
            raise ViewerError("no live frame yet")
        return self._live_jpeg

    def live_decide(
        self,
        goal_xy: tuple[float, float],
        speed_mps: float,
        kind: str,
        instruction: str,
        from_episode: bool = False,
    ) -> dict[str, Any]:
        """Decide on the newest live frame. ``waiting`` says why there is nothing to decide on."""
        if self.live is None:
            raise ViewerError("not connected to a driving service")
        episode = {"armed": self._armed, **self.runner.state()}

        def owns_chair() -> bool:
            # Asked again at the moment of sending: a page's decision that was already under
            # way when an episode began must not reach the chair once the episode has it.
            return from_episode or not self.runner.running()

        if self.runner.running() and not from_episode:
            # An episode is driving. The page watches its decisions; it must not add its own,
            # which would reach the chair as well.
            if self._last_answer is None:
                return {"waiting": "the episode is starting", "episode": episode, "chair": None}
            return {**self._last_answer, "episode": episode}
        sample = self.live.latest()
        status = sample.status
        stats, gps, frame = status.get("stats") or {}, status.get("gps") or {}, status.get("frame")
        link = {
            "service": self.live.service,
            "phone": bool(status.get("phone_connected")),
            "sensors_ok": bool(status.get("sensors_ok")),
            "fps": stats.get("fps"),
            "gps_accuracy_m": gps.get("accuracy_m") if gps.get("fresh") else None,
            # Phone to service, as the service measures it.
            "frame_age_ms": frame.get("age_ms") if frame else None,
        }
        # Shown at the top of the page whatever goal the model is being given.
        ipad = ipad_message(status) if status else None
        waiting = self.live.problem
        if waiting is None and sample.silent_s > 3.0:
            waiting = "the service has gone quiet"
        if waiting is None and not link["phone"]:
            waiting = "the service is up, but no phone is streaming to it"
        if waiting is None and sample.frame is None:
            waiting = "waiting for the first camera frame"
        if waiting is not None:
            self._log_waiting(waiting)
            chair = self._to_chair(None) if owns_chair() else None
            return {"waiting": waiting, "link": link, "ipad": ipad, "chair": chair}

        # What the detector should box: the thing named in the instruction in force.
        said = instruction if kind.startswith("say") else None
        # Kept for a few seconds, so a second page with a point goal cannot blank it.
        if (named := detect.target_phrase(said)) is not None:
            self._target, self._target_at, self._said = named, time.monotonic(), said
        note = None
        if kind == FROM_IPAD:
            # Whatever the rider set on the iPad: a route, a bare destination or an instruction.
            goal, note = self._chair_goal.read(status)
            if goal is None:
                self._log_waiting(note)
                chair = self._to_chair(None) if owns_chair() else None
                return {"waiting": note, "link": link, "ipad": ipad, "chair": chair}
        else:
            goal = self._goal(kind, goal_xy, instruction, self.live_photo)
        with self._lock:
            frames = self.live.window(self.driver.context_frames, self.driver.frame_hz)
            shown = frames[-1]
            decision = self._decision(frames, goal, speed_mps, live=True)
            buffer = io.BytesIO()
            Image.fromarray(shown).save(buffer, format="JPEG", quality=85)
            self._live_jpeg = buffer.getvalue()
        height, width = shown.shape[:2]
        answer = {
            **decision,
            "chair": self._to_chair(decision) if owns_chair() else None,
            "seq": sample.frame.n,
            "width": width,
            "height": height,
            "link": link,
            "goal_note": note,
            "goal_xy": goal.point_xy,
            "ipad": ipad,
            "seen": self.watcher.latest() if self.watcher else None,
            # What the phone and iPad measured at this moment, for the record.
            "sensors": {
                name: status.get(name) for name in ("gps", "heading", "motion", "destination")
            },
            "round_trip_ms": getattr(self.driver, "round_trip_ms", None),
        }
        self._log_decision(answer, kind, instruction)
        self._last_answer = answer
        return {**answer, "episode": episode}

    def decide(
        self,
        index: int,
        goal_xy: tuple[float, float],
        speed_mps: float,
        kind: str = "point",
        instruction: str = "",
        photo_index: int | None = None,
    ) -> dict[str, Any]:
        if not 0 <= index < self.frame_count:
            raise ViewerError(f"frame {index} is outside the video")
        photo = None
        if kind.startswith("photo") and photo_index is not None:
            if not 0 <= photo_index < self.frame_count:
                raise ViewerError("choose a frame as the goal photo first")
            with Image.open(self.frame_path(photo_index)) as image:
                photo = np.asarray(image.convert("RGB"))
        goal = self._goal(kind, goal_xy, instruction, photo)
        key = (
            index, round(goal_xy[0], 2), round(goal_xy[1], 2), round(speed_mps, 2),
            kind, goal.instruction, photo_index if goal.image is not None else None,
        )  # fmt: skip
        with self._lock:
            if key in self._cache:
                return {**self._cache[key], "cached": True}
            decision = {
                "index": index,
                "t_s": round(index / FPS, 2),
                **self._decision(self._window(index), goal, speed_mps),
            }
            self._cache[key] = decision
            return {**decision, "cached": False}


def same_origin(headers: Any) -> bool:
    """A request from this viewer's own page, or from a client that sends no Origin."""
    origin = headers.get("Origin")
    if origin is None:
        return True
    host = headers.get("Host")
    return host is not None and origin in (f"http://{host}", f"https://{host}")


def make_server(session: Session, host: str = "127.0.0.1", port: int = 8200) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, body: dict[str, Any], status: int = 200) -> None:
            self._send(status, json.dumps(body).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/":
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/bench":
                    self._send(200, BENCH_PAGE.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/bench":
                    self._json(session.bench_state())
                elif url.path == "/api/bench/view":
                    self._json(session.bench_view())
                elif url.path == "/api/info":
                    self._json(session.info())
                elif url.path == "/api/models":
                    self._json(session.models())
                elif url.path == "/api/episode":
                    self._json(
                        {
                            "armed": session.armed(),
                            **session.runner.state(),
                            "history": session.runner.history[-10:],
                        }
                    )
                elif url.path == "/api/live/decide":
                    goal = (float(query["gx"][0]), float(query["gy"][0]))
                    self._json(
                        session.live_decide(
                            goal,
                            float(query["speed"][0]),
                            query.get("kind", ["point"])[0],
                            query.get("say", [""])[0],
                        )
                    )
                elif url.path == "/live/now.jpg":
                    self._send(200, session.live_now(), "image/jpeg")
                elif url.path == "/live/frame.jpg":
                    self._send(200, session.live_frame(), "image/jpeg")
                elif url.path == "/api/decide":
                    goal = (float(query["gx"][0]), float(query["gy"][0]))
                    photo = query.get("photo", [""])[0]
                    self._json(
                        session.decide(
                            int(query["i"][0]),
                            goal,
                            float(query["speed"][0]),
                            query.get("kind", ["point"])[0],
                            query.get("say", [""])[0],
                            int(photo) if photo else None,
                        )
                    )
                elif url.path.startswith("/frame/"):
                    index = int(url.path.removeprefix("/frame/").removesuffix(".jpg"))
                    if not 0 <= index < session.frame_count:
                        raise ViewerError(f"frame {index} is outside the video")
                    self._send(200, session.frame_path(index).read_bytes(), "image/jpeg")
                else:
                    self._json({"error": "not found"}, 404)
            except ViewerError as error:
                self._json({"error": str(error)}, 400)
            except (KeyError, ValueError):
                self._json({"error": "bad request"}, 400)

        def do_POST(self) -> None:  # noqa: N802
            """Switch model, or upload a video as the request body (its name comes in the query)."""
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            if url.path == "/api/model":
                # Switch model. Loading one, or waking a hosted one, can take minutes.
                self.rfile.read(length)
                try:
                    session.switch(parse_qs(url.query).get("id", [""])[0])
                    self._json({**session.info(), "models": session.models()})
                except DrivingServiceGiven as given:
                    self._json({"error": str(given), "driving_service": given.service}, 400)
                except ViewerError as error:
                    self._json({"error": str(error)}, 400)
                return
            if url.path.startswith("/api/bench"):
                body = self.rfile.read(length)
                asked = parse_qs(url.query)
                from_page = bool(self.headers.get("Origin")) and same_origin(self.headers)
                try:
                    if url.path == "/api/bench/stop":
                        session.arm(False)
                        session.send_to_chair(False)
                        self._json(session.bench_state())
                    elif not from_page:
                        # Starting a run, or speaking for the person watching, is theirs to do.
                        self._json({"error": "that is done from the bench page"}, 403)
                    elif url.path == "/api/bench/run":
                        self._json(
                            session.bench_run(
                                asked.get("case", [""])[0], asked.get("say", [None])[0]
                            )
                        )
                    elif url.path == "/api/bench/start":
                        self._json(session.set_start())
                    elif url.path == "/api/bench/feedback":
                        said = json.loads(body or b"{}")
                        self._json(
                            session.bench_feedback(
                                str(said.get("run", "")),
                                str(said.get("rating", "")),
                                str(said.get("measured", "")),
                                str(said.get("text", "")),
                            )
                        )
                    else:
                        self._json({"error": "not found"}, 404)
                except (ViewerError, ValueError) as error:
                    self._json({"error": str(error)}, 400)
                return
            if url.path.startswith("/api/episode"):
                self.rfile.read(length)
                asked = parse_qs(url.query)

                def given(name: str, otherwise: float) -> float:
                    try:
                        return float(asked.get(name, [""])[0])
                    except ValueError:
                        return otherwise

                try:
                    if url.path == "/api/episode/arm":
                        on = asked.get("on", ["0"])[0] == "1"
                        # Only a person at the viewer's own page may allow the chair to be
                        # driven: a browser there always says where it is posting from.
                        if on and not (self.headers.get("Origin") and same_origin(self.headers)):
                            self._json({"error": "arming is done from the viewer page"}, 403)
                            return
                        self._json(
                            session.arm(
                                on,
                                given("speed", 40.0),
                                given("brake", 10.0),
                                given("brake_ms", 200.0),
                            )
                        )
                    elif url.path == "/api/episode/go":
                        say = asked.get("say", [""])[0].strip()
                        if not say:
                            raise ViewerError("say what to go to")
                        session.arrive_gap_m = max(0.3, min(2.0, given("gap", 0.6)))
                        limits = Limits(
                            max_steps=int(max(1, min(300, given("steps", 80)))),
                            max_seconds=max(5.0, min(600.0, given("seconds", 120.0))),
                            search=asked.get("search", ["1"])[0] == "1",
                            search_left=asked.get("left", ["1"])[0] == "1",
                        )
                        self._json(session.runner.go(say, limits))
                    elif url.path == "/api/episode/turn":
                        degrees = max(-360.0, min(360.0, given("deg", 0.0)))
                        self._json(session.runner.turn(degrees))
                    elif url.path == "/api/episode/circle":
                        diameter = max(0.4, min(4.0, given("diameter_m", 0.914)))
                        self._json(
                            session.runner.circle(diameter, asked.get("left", ["1"])[0] == "1")
                        )
                    elif url.path == "/api/episode/square":
                        side = max(0.3, min(4.0, given("side_m", 1.219)))
                        self._json(session.runner.square(side, asked.get("left", ["1"])[0] == "1"))
                    elif url.path == "/api/episode/calibrate":
                        v, w = asked.get("v_full", [""])[0], asked.get("w_full", [""])[0]
                        self._json(
                            session.calibrate(float(v) if v else None, float(w) if w else None)
                        )
                    elif url.path == "/api/episode/stop":
                        session.runner.abort("stopped by request")
                        self._json(session.runner.state())
                    else:
                        self._json({"error": "not found"}, 404)
                except (ViewerError, RuntimeError) as error:
                    self._json({"error": str(error)}, 400)
                return
            if url.path.startswith("/api/live"):
                self.rfile.read(length)
                try:
                    if url.path == "/api/live":
                        session.connect_live(parse_qs(url.query).get("service", [""])[0])
                    elif url.path == "/api/live/stop":
                        # Leaving the live view stops the chair but keeps the feed, so
                        # one page going back to video cannot cut off another.
                        session.arm(False)
                        session.send_to_chair(False)
                    elif url.path == "/api/live/chair/settings":
                        asked = parse_qs(url.query)

                        def number(name: str, otherwise: float) -> float:
                            value = asked.get(name, [""])[0]
                            return float(value) if value.isdigit() else otherwise

                        self._json(
                            session.chair_settings(
                                number("speed", 100.0),
                                number("brake", 0.0),
                                number("brake_ms", 0.0),
                            )
                        )
                        return
                    elif url.path in ("/api/live/manual", "/api/live/drive"):
                        # Moves the chair: only this page may.
                        if not (self.headers.get("Origin") and same_origin(self.headers)):
                            self._json({"error": "only the viewer page may do that"}, 403)
                            return
                        asked = parse_qs(url.query)
                        if url.path == "/api/live/manual":
                            self._json(session.manual(asked.get("on", ["0"])[0] == "1"))
                            return
                        if not session._manual:
                            self._json({"error": "keyboard driving is off"}, 400)
                            return

                        def stick(name: str) -> float:
                            try:
                                return max(-30.0, min(30.0, float(asked.get(name, ["0"])[0])))
                            except ValueError:
                                return 0.0

                        problem = session.drive(stick("x"), stick("y"), reverse=True)
                        if problem is None and not session.board().get("chair_listening", True):
                            problem = "the chair is not reading its joystick: is it switched on?"
                            self._json({"sent": True, "warning": problem})
                            return
                        self._json({"sent": problem is None, "problem": problem})
                        return
                    elif url.path == "/api/live/forget":
                        # Empty the obstacle map; it fills again from what is in view.
                        if session.sight is not None:
                            session.sight.forget("Cleared by hand.")
                    elif url.path == "/api/live/relook":
                        # Drop the lock on the target and have the detector find it afresh.
                        if session.watcher is not None:
                            session.watcher.unlock()
                    elif url.path == "/api/live/photo":
                        session.keep_live_photo()
                    elif url.path == "/api/live/chair":
                        # Moves the chair: refuse a page on another site posting here.
                        if not same_origin(self.headers):
                            self._json({"error": "only this page may do that"}, 403)
                            return
                        asked = parse_qs(url.query)
                        on = asked.get("on", ["0"])[0] == "1"
                        if not on:  # the page's stop: also ends any episode and disarms
                            session.arm(False)
                        elif session.runner.running():
                            self._json({"error": "an episode is driving; stop it first"}, 400)
                            return
                        steps = asked.get("steps", [""])[0]
                        speed = asked.get("speed", [""])[0]
                        brake = asked.get("brake", [""])[0]
                        brake_ms = asked.get("brake_ms", [""])[0]
                        self._json(
                            session.send_to_chair(
                                on,
                                int(steps) if steps.isdigit() else None,
                                float(speed) if speed.isdigit() else 100.0,
                                float(brake) if brake.isdigit() else 0.0,
                                float(brake_ms) if brake_ms.isdigit() else 0.0,
                            )
                        )
                        return
                    else:
                        self._json({"error": "not found"}, 404)
                        return
                    self._json({"ok": True})
                except ViewerError as error:
                    self._json({"error": str(error)}, 400)
                return
            if url.path != "/api/video":
                self._json({"error": "not found"}, 404)
                return
            suffix = Path(parse_qs(url.query).get("name", ["video.mp4"])[0]).suffix.lower()
            if suffix not in VIDEO_SUFFIXES or not 0 < length <= MAX_UPLOAD_BYTES:
                self.close_connection = True
                self._json({"error": "send a video file of at most 2 GB"}, 400)
                return
            # Only the extension comes from the browser; the file name is ours.
            target = session.work_dir / f"upload{suffix}"
            remaining = length
            with target.open("wb") as out:
                while remaining:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            try:
                session.open(target)
                session.video_name = Path(parse_qs(url.query)["name"][0]).name
                self._json(session.info())
            except ViewerError as error:
                self._json({"error": str(error)}, 400)

        def log_message(self, *args: object) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("video", nargs="?", type=Path, help="a video file; or drop one on the page")
    parser.add_argument("--driver", help="default: the model used last time, else the stand-in")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--work", type=Path, default=Path("runs/viewer"))
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)

    args.work.mkdir(parents=True, exist_ok=True)
    session = Session(make_driver("fan"), args.work, "fan")
    try:
        wanted = args.driver or json.loads((args.work / "model.json").read_text())
    except (OSError, ValueError):
        wanted = "fan"
    if wanted != "fan":
        try:
            session.switch(wanted)
        except ViewerError as error:
            print(f"could not load {wanted} ({error}); using the stand-in")
    # Pick the chair's feed back up, so a restart does not leave the page disconnected.
    if (service := session.last_service()) is not None:
        try:
            session.connect_live(service)
        except ViewerError as error:
            print(f"could not reconnect to {service}: {error}")
    if args.video is not None:
        session.open(args.video)
    server = make_server(session, port=args.port)
    address = f"http://127.0.0.1:{args.port}"
    print(f"viewer on {address}  (driver: {session.driver.name})")
    if not args.no_open:
        webbrowser.open(address)
    server.serve_forever()


if __name__ == "__main__":
    main()
