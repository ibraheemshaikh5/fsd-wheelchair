"""Watch a model decide, and drive the chair with it.

    uv run python -m fsd_ml.console                 # then choose "Live from chair" on the page
    uv run python -m fsd_ml.console path/to/video.mp4

On a video, the page shows every path the model proposes on each frame, the one the
footprint check chose, and the speed and turn rate that would go to the chair. The video
does not react to the decisions: it is a check of what a model wants, frame by frame.

Live, the same is shown on the chair's camera, and the chair can be driven: by the model
("Send to chair"), by hand from the keyboard, or through the bench page's set moves and the
Claude pilot (``/bench``). Everything that moves the chair goes through one ChairSender, and
every command is written to the run's record (fsd_ml/runlog.py).
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import numpy as np
from PIL import Image

from fsd_ml import bench, chair, obstacles
from fsd_ml.chair_link import ChairOutError, ChairSender, SendResult, to_stick
from fsd_ml.drivers import Candidate, Driver, FanDriver, Goal, Motion, make_driver
from fsd_ml.live import MAX_FRAME_AGE_S, ChairGoal, LiveFeed, LiveFrame, ipad_message
from fsd_ml.moves import Runner
from fsd_ml.pilot import Pilot
from fsd_ml.pipeline.control import ControlConfig, follow
from fsd_ml.pipeline.footprint import FootprintConfig, choose
from fsd_ml.runlog import RunLog
from fsd_ml.video import FPS, Conversion, VideoError, standardize

PAGE = Path(__file__).with_name("page.html")
# Stop for anything this close ahead. Depth reads 10-25% long up close and the chair coasts
# to a stop, so at 0.4 m a bag ended 5 in from the lens; 0.7 m leaves room.
STOP_M = 0.7
BENCH_PAGE = Path(__file__).with_name("bench.html")
MAX_UPLOAD_BYTES = 2 * 1024**3
# How the goal is given: a point, an instruction, a photo, or one of those with a point.
GOAL_KINDS = ("point", "say", "photo", "say+point", "photo+point")
# In the live view only: take the goal the rider set on the iPad.
FROM_IPAD = "ipad"
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
# A model's reply is not used once the frame it was asked about is older than this.
MAX_REPLY_AGE_S = 1.0
# A run ends when the bench page has not asked after it for this long.
PAGE_GONE_S = 2.0
# Frames saved for commands that come many times a second (keyboard, set moves): at most
# one every this long.
TICK_FRAME_GAP_S = 0.1


class ConsoleError(Exception):
    """Something the page should show to the person using it."""


class DrivingServiceGiven(ConsoleError):
    """A driving service's address was given where a model's was expected."""

    def __init__(self, service: str) -> None:
        super().__init__("that address is the driving service, not a model")
        self.service = service


def is_driving_service(url: str) -> bool:
    try:
        answer = httpx.get(f"{url.rstrip('/')}/api/status", timeout=5.0)
        return answer.status_code == 200 and "phone_connected" in answer.text
    except httpx.HTTPError:
        return False


MODEL_TITLES = {"omnivla": "OmniVLA", "fan": "Fan (a stand-in, not a model)"}


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
    """The console's state: the model, a loaded video, the live feed, and the chair."""

    def __init__(self, driver: Driver, work_dir: Path, driver_id: str | None = None) -> None:
        self.driver = driver
        self.driver_id = driver_id or driver.name
        self.work_dir = work_dir
        # Models already loaded, so switching back to one is instant.
        self._loaded: dict[str, Driver] = {self.driver_id: driver}
        self._hosted_file = work_dir / "hosted.json"
        self._service_file = work_dir / "service.json"
        # The camera's place on the chair and the chair's size, from ml/chair.json.
        self.setup = chair.load()
        self.footprint = FootprintConfig(shape=self.setup.chair)
        self.control = ControlConfig()
        # Indoors things are close: react to what is within two metres, stop half a metre short.
        self.avoiding = FootprintConfig(
            shape=self.setup.chair, caution_m=0.15, slow_distance_m=2.0, stop_distance_m=STOP_M
        )
        # How the chair is driven: read afresh from its file every time (fsd_ml/bench.py).
        self.bench = bench.Bench(work_dir / "bench")
        self.runner = Runner(self)

        self.video_name: str | None = None
        self.conversion: Conversion | None = None
        self.frame_count = 0
        self.size = (0, 0)
        self._frames_dir = work_dir / "frames"
        self._cache: dict[tuple, dict[str, Any]] = {}
        # The model runs one call at a time.
        self._lock = threading.Lock()

        self.live: LiveFeed | None = None
        self.live_photo: np.ndarray | None = None
        self._chair_goal: ChairGoal | None = None
        # This connection's record: a line per command, and the frames behind them.
        self.runlog: RunLog | None = None
        self._waiting_logged: str | None = None
        # A depth model that finds what stands on the floor. None if torch is not installed.
        self.sight: obstacles.ObstacleWatcher | None = None
        self._blocked_looks = 0

        # The one sender this console ever makes, kept for its whole life: the board lets one
        # sender drive at a time, and a second from the same console would be locked out.
        self._sender: ChairSender | None = None
        self._sender_lock = threading.Lock()
        # Whether the chair is obeying the console right now, and who in it is driving: a
        # person at the keyboard, a bench run (armed by a press of Run), else the model.
        self.sending = False
        self._manual = False
        self._armed = False
        # Model decisions still allowed to reach the chair; None is no limit.
        self.chair_steps: int | None = None
        # When the bench page last asked how things stand. Runs stop without it.
        self._page_at = 0.0
        # The chair's own count of joystick reads, and when it last went up. A chair that is
        # switched off or asleep stops reading, and then nothing sent to it does anything.
        self._reads: tuple[int | None, float] = (None, 0.0)

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
                raise ConsoleError("a hosted model's address must start with https://")
            driver_id = f"remote:{url.rstrip('/')}"
        elif driver_id not in self._loaded:
            raise ConsoleError(f"no model called {driver_id}")

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
                    raise ConsoleError(reason) from error
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
                raise ConsoleError(str(error)) from error
            self.frame_count = self.conversion.frames
            self.video_name = video.name
            with Image.open(self.frame_path(0)) as first:
                self.size = first.size
            self._cache.clear()

    def frame_path(self, index: int) -> Path:
        return self._frames_dir / f"{index + 1:06d}.jpg"

    def info(self) -> dict[str, Any]:
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
            "chair": asdict(self.setup.chair),
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
            raise ConsoleError("the iPad's goal is only available in the live view")
        if kind not in GOAL_KINDS:
            raise ConsoleError(f"goal type must be one of {', '.join(GOAL_KINDS)}")
        point, wants = "point" in kind, kind.split("+")[0]
        if wants == "say" and not instruction.strip():
            raise ConsoleError("type an instruction first")
        if wants == "photo" and photo is None:
            raise ConsoleError("choose a frame as the goal photo first")
        return Goal(
            point_xy=goal_xy if point else None,
            instruction=instruction.strip() if wants == "say" else None,
            image=photo if wants == "photo" else None,
        )

    def _decision(
        self, frames: list[np.ndarray], goal: Goal, speed_mps: float, live: bool = False
    ) -> dict[str, Any]:
        """Ask the model, check its path against the chair's footprint, and make the command.

        This is the only way a model's path becomes a speed and turn rate: the path, the
        footprint check (against what the obstacle sense sees now, when live), then
        ``follow``.
        """
        started = time.perf_counter()
        candidates = self.driver.propose(frames, goal, Motion(speed_mps, 0.0))
        inference_ms = (time.perf_counter() - started) * 1000.0
        # The model draws from the lens; everything after this is about the chair.
        candidates = [
            Candidate(self.setup.to_chair(c.path_xy), c.score, c.speed_mps) for c in candidates
        ]
        sight = self.sight if live else None
        seen = sight.fresh() if sight else ()
        choice = choose(candidates, seen or (), self.avoiding if sight else self.footprint)
        status, reason = choice.status, choice.reason
        if seen is None:
            # The obstacle sense is on but has no fresh sight of the floor: do not move.
            status, reason, v, w = "blocked", "Cannot see the floor ahead", 0.0, 0.0
        else:
            # Steer by the path as the model drew it: shifting it sideways would pull the
            # chair toward the camera's side for ever. The shift is for the fit check only.
            v, w = follow(
                choice.path_xy - self.setup.lens_xy if len(choice.path_xy) else choice.path_xy,
                choice.speed_factor,
                self.control,
                choice.speed_limit_mps,
            )
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
            "obstacles": [[o.x, o.y, o.radius_m] for o in seen or ()] if sight else None,
            "sight": sight.info if sight else None,
            "v_mps": round(v, 3),
            "w_radps": round(w, 3),
        }

    # Live: the chair's phone, by way of the driving service.

    def connect_live(self, service: str) -> None:
        try:
            feed = LiveFeed(service)
        except ValueError as error:
            raise ConsoleError(str(error)) from error
        self.disconnect_live()
        self.live, self.live_photo = feed, None
        self._chair_goal = ChairGoal(service)
        feed.start()
        self._service_file.write_text(json.dumps(service))
        self.runlog = RunLog(self.work_dir / "live" / time.strftime("%Y%m%d-%H%M%S"))
        self.log("connected", service=service, model=self.driver.name)
        if obstacles.available():
            depth, camera = obstacles.DepthObstacles(), self.setup.camera

            def look() -> np.ndarray | None:
                frame = feed.fresh_frame()
                return frame.image if frame else None

            self.sight = obstacles.ObstacleWatcher(
                look,
                lambda image: depth.find(
                    image, camera.hfov_deg, camera.height_m, self.setup.lens_xy
                ),
            )
            self.sight.start()

    def disconnect_live(self) -> None:
        self.stop("the live connection was closed")
        if self.sight is not None:
            self.sight.stop()
            self.sight = None
        if self.live is not None:
            self.live.stop()
        self.live = None

    def last_service(self) -> str | None:
        """The driving service last connected to, remembered between runs."""
        try:
            return str(json.loads(self._service_file.read_text()))
        except (OSError, ValueError):
            return None

    def keep_live_photo(self) -> None:
        """Use what the camera sees now as the goal photo."""
        sample = self.live.latest() if self.live else None
        if sample is None or sample.frame is None:
            raise ConsoleError("no live frame yet")
        self.live_photo = sample.frame.image

    def live_now(self) -> bytes:
        """The newest picture exactly as the phone sent it, for the page's camera layer."""
        sample = self.live.latest() if self.live else None
        if sample is None or sample.frame is None or not sample.frame.jpeg:
            raise ConsoleError("no live frame yet")
        return sample.frame.jpeg

    # The record of this connection (fsd_ml/runlog.py).

    def log(self, event: str, **fields: Any) -> None:
        if self.runlog is not None:
            self.runlog.write(event, **fields)

    def note(self, event: str, **fields: Any) -> None:
        """A line from the run that is going, for the record."""
        self.log(event, run=self.runner.state().get("id"), **fields)

    def record(
        self,
        mode: str,
        frame: LiveFrame | None,
        result: SendResult | None = None,
        model: dict[str, Any] | None = None,
        frame_gap_s: float = 0.0,
    ) -> None:
        """Write one command line: the frame it was given on, the stick sent, the model's say."""
        if self.runlog is None:
            return
        self.runlog.write(
            "command",
            mode=mode,
            **self.runlog.frame(frame, frame_gap_s),
            stick=[result.x, result.y] if result else None,
            sent=bool(result and result.sent),
            problem=result.problem if result else None,
            model=model,
            board=self._sender.telemetry() if self._sender else None,
        )

    def _log_waiting(self, why: str | None) -> None:
        """Record why nothing was decided, once each time the reason changes."""
        if why != self._waiting_logged:
            self._waiting_logged = why
            self.log("waiting", why=why)

    # The chair. Everything that moves it goes through the one sender, by way of
    # ``drive`` (raw stick: the keyboard and the runner's moves) or ``_send_decision``
    # (the driving model).

    def setting(self, name: str) -> float:
        return self.bench.settings()[name]

    def _engage(self) -> ChairSender:
        """Have the chair obey the console, making the one sender the first time."""
        with self._sender_lock:
            if self._sender is None:
                try:
                    self._sender = ChairSender()
                except ChairOutError as error:
                    raise ConsoleError(str(error)) from error
            self._sender.engage()
            self.sending = True
            return self._sender

    def stop_chair(self) -> dict[str, Any]:
        """Stop the chair now and hand it back to its own joystick. Never waits on a model."""
        was, self.sending = self.sending, False
        self.chair_steps, self._manual = None, False
        sender = self._sender
        if sender is None:
            return {"sending": False}
        settings = self.bench.settings()
        stopped = sender.brake(settings["brake_pct"], settings["brake_ms"])
        sender.release()
        if was:
            self.log("stop", braked_y=stopped.y, board=sender.telemetry())
        return {"sending": False, "chair": asdict(stopped)}

    def stop(self, why: str = "stopped from the page") -> dict[str, Any]:
        """The STOP button: end any run, take away its permission, stop the chair."""
        self._armed = False
        self.runner.abort(why)
        return self.stop_chair()

    def send_model(self, steps: int | None = None) -> dict[str, Any]:
        """Start sending the model's live decisions to the chair.

        With ``steps``, only that many decisions are sent, then the chair is stopped.
        """
        if self.live is None:
            raise ConsoleError("connect to the driving service first")
        if self.runner.running():
            raise ConsoleError("a run is driving; stop it first")
        if self.driver.name == FanDriver.name:
            raise ConsoleError("fan is a stand-in, not a model: pick the real model first")
        self._engage()
        self.chair_steps = steps if steps and steps > 0 else None
        self.log("sending on", steps=self.chair_steps, settings=self.bench.settings())
        return {"sending": True, "steps_left": self.chair_steps}

    def manual(self, on: bool) -> dict[str, Any]:
        """Switch driving by hand from the page's keyboard on or off."""
        if not on:
            if self._manual:
                self.stop_chair()
                self.log("manual off")
            return {"manual": False}
        if self.runner.running():
            raise ConsoleError("a run is driving; stop it first")
        if self.live is None:
            raise ConsoleError("connect to the driving service first")
        self._engage()
        self._manual, self.chair_steps = True, None
        self.log("manual on")
        return {"manual": True}

    def _mode(self) -> str:
        """Who is driving, as the record names it."""
        if self._manual:
            return "manual"
        if self.runner.running():
            return "pilot" if self.runner.state().get("kind") == "pilot" else "bench"
        return "model"

    def drive(self, x_pct: float, y_pct: float, reverse: bool = False) -> str | None:
        """Send a raw stick position (x right, y forward). A reason if it was not sent."""
        sender = self._sender
        if not self.sending or sender is None:
            return "sending was switched off"
        result = sender.send_stick(x_pct, y_pct, reverse)
        self.record(
            self._mode(),
            self.live.latest().frame if self.live else None,
            result,
            model=self.runner.state().get("call"),
            frame_gap_s=TICK_FRAME_GAP_S,
        )
        if result.halted:  # the board took control back: a person must switch it on again
            self._armed = False
            self.stop_chair()
            return result.problem
        return None

    def _send_decision(
        self, decision: dict[str, Any] | None, frame: LiveFrame | None, model: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Send a model's decision, or stop when there is none, if the model is driving."""
        sender = self._sender
        driving = self.sending and sender is not None and self._mode() == "model"
        if decision is None:
            if not driving:
                return None
            settings = self.bench.settings()
            stopped = sender.brake(settings["brake_pct"], settings["brake_ms"])
            return {**asdict(stopped), "v_mps": 0.0, "w_radps": 0.0, "board": self.board()}
        if not driving:
            # Watched, not obeyed. While someone else is driving, what the model would have
            # done is recorded beside what they did.
            if self.sending:
                self.record("model", frame, model=model)
            return {"sent": False, "manual": True, "board": self.board()} if self._manual else None
        if self.chair_steps is not None and self.chair_steps <= 0:
            self.stop_chair()
            return {"sent": False, "halted": True, "problem": "the steps asked for are done"}
        settings = self.bench.settings()
        x, y = to_stick(
            decision["v_mps"], decision["w_radps"], settings["v_full_mps"], settings["w_full_radps"]
        )
        # Scaled after the stick cap, so the setting slows the chair even when the cap binds.
        scale = max(0.1, min(1.0, settings["model_speed_pct"] / 100.0))
        result = sender.send_stick(x * scale, y * scale)
        self.record("model", frame, result, model)
        if result.halted:  # the board took control back: the rider must tick again
            self.stop_chair()
        elif self.chair_steps is not None:
            self.chair_steps -= 1
        return {
            **asdict(result),
            "v_mps": decision["v_mps"],
            "w_radps": decision["w_radps"],
            "steps_left": self.chair_steps,
            "board": self.board(),
        }

    def board(self) -> dict[str, Any]:
        """The board's telemetry, with whether the chair is listening to its joystick at all."""
        sender = self._sender
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

    # What the runner needs (fsd_ml/moves.py), beyond ``drive``, ``record``, ``note``
    # and ``setting`` above.

    def armed(self) -> bool:
        return self._armed

    def page_alive(self) -> bool:
        return time.monotonic() - self._page_at < PAGE_GONE_S

    def begin(self) -> str | None:
        try:
            self._engage()
        except ConsoleError as error:
            return str(error)
        self._manual, self.chair_steps = False, None
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
        self.stop_chair()

    def finished(self, record: dict[str, Any]) -> None:
        """A run is over: write how it went, and take its permission away."""
        self._armed = False
        self.log("run finished", **{k: v for k, v in record.items() if k != "call"})

    def hold(self) -> None:
        """Stand still for now, staying ready to move again."""
        if self.sending and self._sender is not None:
            settings = self.bench.settings()
            self._sender.brake(settings["brake_pct"], settings["brake_ms"])

    def yaw_rad(self) -> float:
        return self.live.yaw_rad() if self.live else 0.0

    def gyro_fresh(self) -> bool:
        return self.live is not None and self.live.gyro_fresh()

    def speed_for(self, y_pct: float) -> float:
        return max(0.0, y_pct) / 100 * self.setting("v_full_mps")

    def observe(self) -> tuple[LiveFrame | None, str]:
        """A fresh camera frame for the pilot, and what the obstacle sense finds, in plain lines."""
        frame = self.live.fresh_frame() if self.live else None
        if frame is None or not frame.jpeg:
            return None, ""
        seen = self.sight.fresh() if self.sight else None
        if self.sight is None:
            return frame, "The chair has no obstacle sense running: go by the picture alone."
        if seen is None:
            return frame, "The chair's obstacle sense cannot see the floor right now."
        body = self.setup.chair
        front, half = body.length_m / 2, body.width_m / 2

        def said(where: str, low: float, high: float) -> str:
            gaps = [o.x - front for o in seen if o.x > front - 0.1 and low <= o.y <= high]
            if not gaps:
                return f"- {where}: nothing sensed within 3 m."
            return f"- {where}: something about {max(0.0, min(gaps)):.1f} m from the chair's front."

        return frame, "\n".join(
            [
                "The chair's obstacle sense (rough, and blind closer than 0.7 m):",
                said("Straight ahead", -half - 0.15, half + 0.15),
                said("Ahead to the left", half + 0.15, half + 0.9),
                said("Ahead to the right", -half - 0.9, -half - 0.15),
            ]
        )

    def blocked(self) -> str | None:
        """Whether the next metre straight ahead meets an obstacle. None when it is clear,
        and when no obstacle sense is installed."""
        if self.sight is None:
            return None
        seen = self.sight.fresh()
        if seen is None:
            return "cannot see the floor ahead"
        ahead = np.arange(0.1, 1.0 + 1e-9, 0.1)
        # A set move is short and watched, so it keeps a hand's width clear, not the full
        # clearance used when a model picks its own way.
        tight = FootprintConfig(
            shape=chair.Body(
                self.setup.chair.width_m, self.setup.chair.length_m, self.setting("clearance_m")
            ),
            caution_m=0.05,
            slow_distance_m=2.0,
            stop_distance_m=STOP_M,
        )
        picked = choose(
            [Candidate(np.column_stack([ahead, np.zeros_like(ahead)]), 1.0)], seen, tight
        )
        if picked.status != "blocked":
            self._blocked_looks = 0
            return None
        # One look can be wrong, most of all while the chair pitches as it sets off. Three in
        # a row, about a third of a second, is something really there.
        self._blocked_looks += 1
        if self._blocked_looks < 3:
            return None
        near = min(seen, key=lambda o: o.x * o.x + o.y * o.y, default=None)
        if near is None:
            return picked.reason or "obstacle"
        side = "left" if near.y > 0 else "right"
        return f"{picked.reason or 'obstacle'} ({near.x:.1f} m ahead, {abs(near.y):.1f} m {side})"

    # The bench page.

    def bench_run(self, case_id: str, say: str | None = None) -> dict[str, Any]:
        """Start one of the bench's runs. The press of Run on the page is the permission."""
        chosen = bench.case(case_id)
        if chosen is None:
            raise ConsoleError(f"no test called {case_id}")
        if self.runner.running():
            raise ConsoleError("a run is already going; stop it first")
        if self.live is None:
            raise ConsoleError("not connected to the chair's driving service")
        params = dict(chosen["params"])
        if chosen.get("input") == "say" and say and say.strip():
            params["say"] = say.strip()
        if chosen["kind"] == "pilot" and not os.environ.get("ANTHROPIC_API_KEY"):
            raise ConsoleError(
                "no Claude API key: put ANTHROPIC_API_KEY in ml/.env and start the console"
                " with uv run --env-file .env"
            )
        if self.sending:
            self.stop_chair()  # whoever was driving (keyboard or model) gives way to the run
        self._armed, self._page_at = True, time.monotonic()
        self.log("run", case=case_id, params=params, settings=self.bench.settings())
        try:
            if chosen["kind"] == "straight":
                self.runner.straight(params["distance_m"])
            elif chosen["kind"] == "turn":
                self.runner.turn(params["degrees_left"])
            else:
                self.runner.pilot(
                    params["say"],
                    Pilot(params["say"]),
                    max_turns=max(1, round(self.setting("pilot_max_looks"))),
                )
        except RuntimeError as error:
            self._armed = False
            raise ConsoleError(str(error)) from error
        return self.bench_state()

    def bench_state(self) -> dict[str, Any]:
        """Everything the bench page shows that is not the camera's picture."""
        waiting = "not connected to the chair's driving service"
        if self.live is not None:
            sample = self.live.latest()
            waiting = self.live.problem
            if waiting is None and not sample.status.get("phone_connected"):
                waiting = "no phone is streaming"
            if waiting is None and sample.frame is None:
                waiting = "waiting for the first camera frame"
        return {
            "cases": bench.CASES,
            "settings": self.bench.settings(),
            "run": self.runner.state(),
            "manual": self._manual,
            "waiting": waiting,
            "gyro_fresh": self.gyro_fresh(),
            "sight": self.sight.info if self.sight else None,
            "obstacles": [[o.x, o.y, o.radius_m] for o in self.sight.obstacles]
            if self.sight
            else [],
            "chair": asdict(self.setup.chair),
            "board": self.board() if self.sending else {},
        }

    def live_decide(
        self,
        goal_xy: tuple[float, float],
        speed_mps: float,
        kind: str,
        instruction: str,
    ) -> dict[str, Any]:
        """Decide on the newest live frame, send the decision if the model is driving, and
        record it. ``waiting`` says why there is nothing to decide on; the chair is then
        stopped."""
        if self.live is None:
            raise ConsoleError("not connected to a driving service")
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

        def wait(why: str) -> dict[str, Any]:
            self._log_waiting(why)
            chair_link = self._send_decision(None, None, {})
            return {"waiting": why, "link": link, "ipad": ipad, "chair": chair_link}

        waiting = self.live.problem
        if waiting is None and sample.silent_s > 3.0:
            waiting = "the service has gone quiet"
        if waiting is None and not link["phone"]:
            waiting = "the service is up, but no phone is streaming to it"
        if waiting is None and sample.frame is None:
            waiting = "waiting for the first camera frame"
        if waiting is not None:
            return wait(waiting)

        note = None
        if kind == FROM_IPAD:
            # Whatever the rider set on the iPad: a route, a bare destination or an instruction.
            goal, note = self._chair_goal.read(status)
            if goal is None:
                return wait(note)
        else:
            goal = self._goal(kind, goal_xy, instruction, self.live_photo)
        with self._lock:
            # The frames are picked once the model is free, so the decision is on the newest.
            frames = self.live.window(self.driver.context_frames, self.driver.frame_hz)
            used = frames[-1]
            age_s = used.age_s(time.monotonic())
            if age_s > MAX_FRAME_AGE_S:
                return wait(f"the newest camera frame is {age_s * 1000:.0f} ms old")
            try:
                decision = self._decision([f.image for f in frames], goal, speed_mps, live=True)
            except httpx.HTTPError as error:
                return wait(f"the model did not answer in time ({type(error).__name__})")
        age_s = used.age_s(time.monotonic())
        if age_s > MAX_REPLY_AGE_S:
            # The chair has moved on from that frame: stop rather than act on it.
            decision = {
                **decision,
                "status": "blocked",
                "reason": f"Decision too old to use ({age_s * 1000:.0f} ms)",
                "v_mps": 0.0,
                "w_radps": 0.0,
            }
        self._waiting_logged = None
        chosen, candidates = decision["chosen"], decision["candidates"]
        picked = candidates[chosen] if chosen is not None else {}
        round_trip_ms = getattr(self.driver, "round_trip_ms", None)
        model = {
            "name": self.driver.name,
            "goal_kind": kind,
            "say": goal.instruction,
            "goal_xy": goal.point_xy,
            # In the chair's frame; the model drew it from the lens, lens_xy further back.
            "path": picked.get("path"),
            "lens_xy": [round(v, 3) for v in self.setup.lens_xy],
            "speed_mps": picked.get("speed_mps"),
            "status": decision["status"],
            "reason": decision["reason"],
            "v_mps": decision["v_mps"],
            "w_radps": decision["w_radps"],
            "inference_ms": decision["inference_ms"],
            "round_trip_ms": round_trip_ms,
            "obstacles": decision["obstacles"],
            # What the phone measured at this moment.
            "sensors": {name: status.get(name) for name in ("gps", "heading", "motion")},
        }
        height, width = used.image.shape[:2]
        return {
            **decision,
            "chair": self._send_decision(decision, used, model),
            "seq": used.n,
            "width": width,
            "height": height,
            "link": link,
            "goal_note": note,
            "goal_xy": goal.point_xy,
            "ipad": ipad,
            "round_trip_ms": round_trip_ms,
        }

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
            raise ConsoleError(f"frame {index} is outside the video")
        photo = None
        if kind.startswith("photo") and photo_index is not None:
            if not 0 <= photo_index < self.frame_count:
                raise ConsoleError("choose a frame as the goal photo first")
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
            try:
                decision = {
                    "index": index,
                    "t_s": round(index / FPS, 2),
                    **self._decision(self._window(index), goal, speed_mps),
                }
            except httpx.HTTPError as error:
                raise ConsoleError(
                    f"the model did not answer in time ({type(error).__name__});"
                    " if it was asleep it is waking, try again"
                ) from error
            self._cache[key] = decision
            return {**decision, "cached": False}


def from_page(headers: Any) -> bool:
    """Whether a request came from this console's own page.

    A browser always says where a POST comes from, so a missing Origin is a script, and
    one that does not match is another site. Neither may move the chair.
    """
    origin, host = headers.get("Origin"), headers.get("Host")
    return bool(origin and host) and origin in (f"http://{host}", f"https://{host}")


# Routes that move the chair or let it be moved: only this console's own page may call them.
# The two switches are guarded when switching on; anyone may switch off or stop.
MOVING = {"/api/bench/run", "/api/bench/set", "/api/live/decide", "/api/live/drive"}
SWITCHES = {"/api/live/chair", "/api/live/manual"}


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
                    # The bench page's poll is also its heartbeat: a run stops without it.
                    session._page_at = time.monotonic()
                    self._json(session.bench_state())
                elif url.path == "/api/info":
                    self._json(session.info())
                elif url.path == "/api/models":
                    self._json(session.models())
                elif url.path == "/live/now.jpg":
                    self._send(200, session.live_now(), "image/jpeg")
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
                        raise ConsoleError(f"frame {index} is outside the video")
                    self._send(200, session.frame_path(index).read_bytes(), "image/jpeg")
                else:
                    self._json({"error": "not found"}, 404)
            except ConsoleError as error:
                self._json({"error": str(error)}, 400)
            except (KeyError, ValueError):
                self._json({"error": "bad request"}, 400)

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/api/video":
                self._upload(url)
                return
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            asked = parse_qs(url.query)

            def given(name: str, otherwise: str = "") -> str:
                return asked.get(name, [otherwise])[0]

            on = given("on", "0") == "1"
            if (url.path in MOVING or (url.path in SWITCHES and on)) and not from_page(
                self.headers
            ):
                self._json({"error": "only the console's own page may do that"}, 403)
                return
            try:
                if url.path == "/api/model":
                    # Switch model. Loading one, or waking a hosted one, can take minutes.
                    session.switch(given("id"))
                    self._json({**session.info(), "models": session.models()})
                elif url.path == "/api/bench/run":
                    self._json(session.bench_run(given("case"), given("say") or None))
                elif url.path == "/api/bench/set":
                    session.bench.set(**{given("name"): float(given("value"))})
                    self._json(session.bench_state())
                elif url.path == "/api/bench/stop":
                    session.stop()
                    self._json(session.bench_state())
                elif url.path == "/api/live":
                    session.connect_live(given("service"))
                    self._json({"ok": True})
                elif url.path == "/api/live/stop":
                    # Leaving the live view stops the chair but keeps the feed, so
                    # one page going back to video cannot cut off another.
                    session.stop()
                    self._json({"ok": True})
                elif url.path == "/api/live/photo":
                    session.keep_live_photo()
                    self._json({"ok": True})
                elif url.path == "/api/live/decide":
                    self._json(
                        session.live_decide(
                            (float(given("gx")), float(given("gy"))),
                            float(given("speed")),
                            given("kind", "point"),
                            given("say"),
                        )
                    )
                elif url.path == "/api/live/chair":
                    if on:
                        steps = given("steps")
                        self._json(session.send_model(int(steps) if steps.isdigit() else None))
                    else:  # the page's STOP: also ends any run
                        self._json(session.stop())
                elif url.path == "/api/live/manual":
                    self._json(session.manual(on))
                elif url.path == "/api/live/drive":
                    if not session._manual:
                        raise ConsoleError("keyboard driving is off")
                    # A person at the keyboard may back up. The stick is capped by the sender.
                    problem = session.drive(float(given("x", "0")), float(given("y", "0")), True)
                    warning = None
                    if problem is None and not session.board().get("chair_listening", True):
                        warning = "the chair is not reading its joystick: is it switched on?"
                    self._json({"sent": problem is None, "problem": problem, "warning": warning})
                else:
                    self._json({"error": "not found"}, 404)
            except DrivingServiceGiven as service:
                self._json({"error": str(service), "driving_service": service.service}, 400)
            except ConsoleError as error:
                self._json({"error": str(error)}, 400)
            except (KeyError, ValueError) as error:
                self._json({"error": f"bad request: {error}"}, 400)

        def _upload(self, url: Any) -> None:
            """A video as the request body; its name comes in the query."""
            length = int(self.headers.get("Content-Length", 0))
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
            except ConsoleError as error:
                self._json({"error": str(error)}, 400)

        def log_message(self, *args: object) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("video", nargs="?", type=Path, help="a video file; or drop one on the page")
    parser.add_argument("--driver", help="remote:<url>; default is the model used last time")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--work", type=Path, default=Path("runs/console"))
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
        except ConsoleError as error:
            print(f"could not load {wanted} ({error}); using the stand-in")
    # Pick the chair's feed back up, so a restart does not leave the page disconnected.
    if (service := session.last_service()) is not None:
        try:
            session.connect_live(service)
        except ConsoleError as error:
            print(f"could not reconnect to {service}: {error}")
    if args.video is not None:
        session.open(args.video)
    server = make_server(session, port=args.port)
    address = f"http://127.0.0.1:{args.port}"
    print(f"console on {address}  (driver: {session.driver.name})")
    if not args.no_open:
        webbrowser.open(address)
    server.serve_forever()


if __name__ == "__main__":
    main()
