"""Watch a model decide, frame by frame, on a video.

    uv run --group onnx python -m fsd_ml.viewer path/to/video.mp4

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
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import numpy as np
from PIL import Image

from fsd_ml.drivers import Driver, Goal, Motion, make_driver
from fsd_ml.flowpilot import ONNX_PATH as FLOWPILOT_WEIGHTS
from fsd_ml.live import ChairGoal, LiveFeed
from fsd_ml.pipeline.control import ControlConfig, follow
from fsd_ml.pipeline.footprint import FootprintConfig, choose
from fsd_ml.s2e import ONNX_PATH as S2E_WEIGHTS
from fsd_ml.video import FPS, Conversion, VideoError, standardize

PAGE = Path(__file__).with_name("page.html")
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


def is_driving_service(url: str) -> bool:
    try:
        answer = httpx.get(f"{url.rstrip('/')}/api/status", timeout=5.0)
        return answer.status_code == 200 and "phone_connected" in answer.text
    except httpx.HTTPError:
        return False


# Models that run on this computer, and the file each needs.
LOCAL_MODELS = {"flowpilot": FLOWPILOT_WEIGHTS, "s2e": S2E_WEIGHTS}

MODEL_TITLES = {
    "omnivla": "OmniVLA",
    "flowpilot": "FlowPilot (small)",
    "s2e": "S2E",
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
        self.footprint = FootprintConfig()
        self.control = ControlConfig()
        self.video_name: str | None = None
        self.conversion: Conversion | None = None
        self.live: LiveFeed | None = None
        self.live_photo: np.ndarray | None = None
        self._live_jpeg: bytes | None = None
        self._chair_goal: ChairGoal | None = None
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
        """What the page can switch to: local models, then hosted ones."""
        options = []
        for name, weights in LOCAL_MODELS.items():
            ready = weights.is_file()
            options.append(
                {
                    "id": name,
                    "label": self._label(name, f"{MODEL_TITLES[name]} · this computer"),
                    "ready": ready,
                    "hint": None if ready else f"download first: python -m fsd_ml.weights {name}",
                }
            )
        for url in self.hosted():
            options.append(
                {
                    "id": f"remote:{url}",
                    "label": self._label(
                        f"remote:{url}", f"Hosted model · {urlparse(url).hostname}"
                    ),
                    "ready": True,
                    "hint": None,
                }
            )
        if self.driver_id not in {option["id"] for option in options}:
            label = self._label(self.driver_id, self.driver_id)
            options.insert(0, {"id": self.driver_id, "label": label, "ready": True, "hint": None})
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
        elif driver_id not in LOCAL_MODELS and driver_id not in self._loaded:
            raise ViewerError(f"no model called {driver_id}")

        with self._lock:
            if driver_id not in self._loaded:
                try:
                    self._loaded[driver_id] = make_driver(driver_id)
                except FileNotFoundError as error:
                    raise ViewerError(str(error)) from error
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

    def _decision(self, frames: list[np.ndarray], goal: Goal, speed_mps: float) -> dict[str, Any]:
        """Ask the model, then choose a path and a command as the pipeline would."""
        started = time.perf_counter()
        candidates = self.driver.propose(frames, goal, Motion(speed_mps, 0.0))
        inference_ms = (time.perf_counter() - started) * 1000.0

        choice = choose(candidates, [], self.footprint)
        v, w = follow(
            choice.path_xy,
            choice.speed_factor,
            speed_mps,
            1.0 / FPS,
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
            "status": choice.status,
            "reason": choice.reason,
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

    def last_service(self) -> str | None:
        """The driving service last connected to, remembered between runs."""
        try:
            return str(json.loads(self._service_file.read_text()))
        except (OSError, ValueError):
            return None

    def disconnect_live(self) -> None:
        if self.live is not None:
            self.live.stop()
        self.live = self._live_jpeg = None

    def keep_live_photo(self) -> None:
        """Use what the camera sees now as the goal photo."""
        sample = self.live.latest() if self.live else None
        if sample is None or sample.frame is None:
            raise ViewerError("no live frame yet")
        self.live_photo = sample.frame.image

    def live_frame(self) -> bytes:
        if self._live_jpeg is None:
            raise ViewerError("no live frame yet")
        return self._live_jpeg

    def live_decide(
        self, goal_xy: tuple[float, float], speed_mps: float, kind: str, instruction: str
    ) -> dict[str, Any]:
        """Decide on the newest live frame. ``waiting`` says why there is nothing to decide on."""
        if self.live is None:
            raise ViewerError("not connected to a driving service")
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
        waiting = self.live.problem
        if waiting is None and sample.silent_s > 3.0:
            waiting = "the service has gone quiet"
        if waiting is None and not link["phone"]:
            waiting = "the service is up, but no phone is streaming to it"
        if waiting is None and sample.frame is None:
            waiting = "waiting for the first camera frame"
        if waiting is not None:
            return {"waiting": waiting, "link": link}

        note = None
        if kind == FROM_IPAD:
            # Whatever the rider set on the iPad: a route, a bare destination or an instruction.
            goal, note = self._chair_goal.read(status)
            if goal is None:
                return {"waiting": note, "link": link}
        else:
            goal = self._goal(kind, goal_xy, instruction, self.live_photo)
        with self._lock:
            frames = self.live.window(self.driver.context_frames, self.driver.frame_hz)
            shown = frames[-1]
            decision = self._decision(frames, goal, speed_mps)
            buffer = io.BytesIO()
            Image.fromarray(shown).save(buffer, format="JPEG", quality=85)
            self._live_jpeg = buffer.getvalue()
        height, width = shown.shape[:2]
        return {
            **decision,
            "seq": sample.frame.n,
            "width": width,
            "height": height,
            "link": link,
            "goal_note": note,
            "goal_xy": goal.point_xy,
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
                elif url.path == "/api/info":
                    self._json(session.info())
                elif url.path == "/api/models":
                    self._json(session.models())
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
            if url.path.startswith("/api/live"):
                self.rfile.read(length)
                try:
                    if url.path == "/api/live":
                        session.connect_live(parse_qs(url.query).get("service", [""])[0])
                    elif url.path == "/api/live/stop":
                        session.disconnect_live()
                    elif url.path == "/api/live/photo":
                        session.keep_live_photo()
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
    parser.add_argument("--driver", default="flowpilot")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--work", type=Path, default=Path("runs/viewer"))
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)

    args.work.mkdir(parents=True, exist_ok=True)
    session = Session(make_driver(args.driver), args.work, args.driver)
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
