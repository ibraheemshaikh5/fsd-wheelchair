"""The chair's phone, live, by way of the driving service.

The service already offers a feed for viewers (``/ws/viewer``): about ten times
a second it sends its status as JSON, then the newest camera frame as a JPEG if
there is a new one. This connects to that feed as one more viewer, so the
service needs no changes. The service allows four viewers at once.

    feed = LiveFeed("http://127.0.0.1:8000")      # or the service's Tailscale address
    feed.start()
    sample = feed.latest()
"""

from __future__ import annotations

import io
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import numpy as np
from PIL import Image

from fsd_ml.drivers import Goal
from fsd_ml.geo import to_local, world_to_chair
from fsd_ml.pipeline.route import Route
from fsd_ml.pipeline.types import Sensors
from fsd_ml.video import standardize_frame

# How much recent footage is kept for models that read a history.
HISTORY_S = 4.0
TOO_MANY_VIEWERS = 4429


@dataclass(frozen=True, slots=True)
class LiveFrame:
    # Counts up by one for every new frame received.
    n: int
    # When it arrived here, on this machine's monotonic clock.
    received_s: float
    # In the form the models are fed (see video.py).
    image: np.ndarray


@dataclass(frozen=True, slots=True)
class LiveSample:
    status: dict[str, Any]
    frame: LiveFrame | None
    # Seconds since anything arrived from the service.
    silent_s: float


def feed_address(service: str) -> str:
    """``http(s)://host[:port]`` of the driving service as its viewer feed's address."""
    parts = urlparse(service if "://" in service else f"http://{service}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the service address should look like http://127.0.0.1:8000")
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}/ws/viewer"


class LiveFeed:
    """Keeps the newest status and the last few seconds of frames from the service."""

    def __init__(self, service: str) -> None:
        self.service = service
        self.address = feed_address(service)
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {}
        self._frames: deque[LiveFrame] = deque()
        self._count = 0
        self._heard_s: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Why the feed is down, in words, while it is.
        self.problem: str | None = "connecting"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from websockets.exceptions import ConnectionClosed, InvalidStatus
        from websockets.sync.client import connect

        wait_s = 0.5
        while not self._stop.is_set():
            try:
                with connect(self.address, open_timeout=5, max_size=8 * 1024 * 1024) as socket:
                    while not self._stop.is_set():
                        try:
                            message = socket.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        self._take(message)
                        wait_s = 0.5
            except ConnectionClosed as closed:
                code = closed.rcvd.code if closed.rcvd else None
                self.problem = (
                    "the service already has four viewers; close one"
                    if code == TOO_MANY_VIEWERS
                    else "the service closed the connection"
                )
            except InvalidStatus as refused:
                self.problem = (
                    f"the service refused the connection ({refused.response.status_code})"
                )
            except (OSError, TimeoutError) as error:
                self.problem = f"cannot reach the service: {error}"
            self._stop.wait(wait_s)
            wait_s = min(wait_s * 2, 5.0)

    def _take(self, message: str | bytes) -> None:
        now = time.monotonic()
        if isinstance(message, str):
            try:
                status = json.loads(message)
            except ValueError:
                return
            with self._lock:
                self._status, self._heard_s, self.problem = status, now, None
            return
        try:
            with Image.open(io.BytesIO(message)) as image:
                frame = standardize_frame(np.asarray(image.convert("RGB")))
        except OSError:
            return
        with self._lock:
            self._count += 1
            self._frames.append(LiveFrame(self._count, now, frame))
            while self._frames and now - self._frames[0].received_s > HISTORY_S:
                self._frames.popleft()
            self._heard_s = now

    def latest(self) -> LiveSample:
        with self._lock:
            frame = self._frames[-1] if self._frames else None
            silent = float("inf") if self._heard_s is None else time.monotonic() - self._heard_s
            return LiveSample(dict(self._status), frame, silent)

    def window(self, count: int, frame_hz: float) -> list[np.ndarray]:
        """The newest ``count`` frames about ``1 / frame_hz`` apart, oldest first.

        The feed is slower than some models' frame rate, so the nearest frame in
        time stands in, and a short history repeats its oldest frame.
        """
        with self._lock:
            frames = list(self._frames)
        if not frames:
            return []
        newest = frames[-1].received_s
        picked = []
        for back in range(count):
            wanted = newest - back / frame_hz
            picked.append(min(frames, key=lambda f: abs(f.received_s - wanted)).image)
        return picked[::-1]


def to_sensors(sample: LiveSample, now_ms: float) -> Sensors:
    """A live sample as the pipeline's input. Ages come from the service's own status."""
    status = sample.status
    frame, gps, heading = status.get("frame"), status.get("gps"), status.get("heading") or {}

    def taken(reading: dict[str, Any] | None) -> float | None:
        # Only a reading the service calls fresh gets a time; a stale one counts as missing.
        if not reading or not reading.get("fresh") or reading.get("age_ms") is None:
            return None
        return now_ms - float(reading["age_ms"])

    # Only a heading against true north can be combined with a map route.
    true_heading = heading.get("deg") if heading.get("reference") == "true" else None
    return Sensors(
        t_ms=now_ms,
        frame=sample.frame.image if sample.frame else None,
        frame_t_ms=taken(frame),
        lat=gps.get("lat") if gps else None,
        lon=gps.get("lon") if gps else None,
        fix_t_ms=taken(gps),
        heading_deg=true_heading,
        speed_mps=gps.get("speed_mps") if gps else None,
    )


# How far along the route the model is aimed.
ROUTE_LOOKAHEAD_M = 10.0


class ChairGoal:
    """The trip the rider set on the iPad, as a goal for a model.

    The service's status says what kind of goal there is and gives each change a
    new version number; the route itself is fetched once per version.
    """

    def __init__(self, service: str) -> None:
        self._service = service.rstrip("/")
        self._version: int | None = None
        self._route: Route | None = None
        self._progress_m = 0.0

    def _route_for(self, version: int) -> Route | None:
        if version != self._version:
            answer = httpx.get(f"{self._service}/api/goal", timeout=5.0).raise_for_status().json()
            points = (answer.get("data") or {}).get("route") or []
            self._route = (
                Route([(p["lat"], p["lon"]) for p in points]) if len(points) >= 2 else None
            )
            self._version, self._progress_m = version, 0.0
        return self._route

    def read(self, status: dict[str, Any]) -> tuple[Goal | None, str]:
        """The goal, and a line saying what it is; or None and why there is none yet."""
        goal, gps, heading = status.get("goal"), status.get("gps"), status.get("heading") or {}
        if goal and goal.get("kind") == "instruction":
            return Goal(instruction=goal["instruction"]), f"iPad: “{goal['instruction']}”"

        destination = status.get("destination")
        if not goal and not destination:
            return None, "no destination set on the iPad yet"

        # Everything from here needs to know where the chair is and which way it faces.
        placed = gps and gps.get("fresh") and heading.get("reference") == "true"
        if not placed or heading.get("deg") is None:
            return None, "the iPad has a destination, but the chair has no fresh GPS and heading"
        facing = float(heading["deg"])

        if goal and goal.get("kind") == "route":
            try:
                route = self._route_for(int(goal["version"]))
            except (httpx.HTTPError, KeyError, ValueError) as error:
                return None, f"could not fetch the route from the service: {error}"
            if route is not None:
                east, north = to_local(gps["lat"], gps["lon"], route.origin)
                self._progress_m = route.project(east, north, self._progress_m)
                left_m = route.length_m - self._progress_m
                target = route.point_at(self._progress_m + ROUTE_LOOKAHEAD_M)
                ahead, left = world_to_chair(target, east, north, facing)[0]
                return Goal(
                    point_xy=(float(ahead), float(left))
                ), f"iPad route: {left_m:.0f} m left"

        # A bare destination: aim straight at it.
        distance, bearing = destination.get("distance_m"), destination.get("bearing_deg")
        if distance is None or bearing is None:
            return None, "the iPad has a destination, but the chair has no fresh GPS"
        off = math.radians(float(bearing) - facing)
        point = (float(distance) * math.cos(off), -float(distance) * math.sin(off))
        return Goal(point_xy=point), f"iPad destination: {float(distance):.0f} m away, no route"
