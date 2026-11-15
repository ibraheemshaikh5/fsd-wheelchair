"""The chair's phone, live, by way of the driving service.

This reads the service's model feed (``/ws/model``), not the console feed the
iPad uses: the service pushes every camera frame the moment it lands, as the
status (JSON) and then the frame (``[uint32 LE header length][header JSON][JPEG]``).
The header's ``age_ms`` says how long ago the phone captured the frame, so each
frame is placed at its capture time on this machine's clock, and a model's
frames are spaced by when they were taken, not by when a busy link delivered
them. The service allows two model feeds at once.

    feed = LiveFeed("http://127.0.0.1:8000")      # or the service's Tailscale address
    feed.start()
    sample = feed.latest()

The feed also keeps count of how far the chair has turned, from the phone's gyro in each
status, so anything that turns the chair by an angle can read it at any time.
"""

from __future__ import annotations

import io
import json
import math
import ssl
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import certifi
import httpx
import numpy as np
from PIL import Image

from fsd_ml.drivers import Goal
from fsd_ml.geo import to_local, world_to_chair
from fsd_ml.pipeline.route import Route
from fsd_ml.video import standardize_frame

# How much recent footage is kept for models that read a history.
HISTORY_S = 4.0
# A frame older than this is not driven on: the chair has moved since it was taken.
MAX_FRAME_AGE_S = 0.5
# The gyro counts as reporting while a fresh reading arrived within this long.
GYRO_FRESH_S = 0.5
MS_PER_S = 1000.0
_LENGTH_PREFIX = struct.Struct("<I")


@dataclass(frozen=True, slots=True)
class LiveFrame:
    # Counts up by one for every new frame received.
    n: int
    # When it arrived here, on this machine's monotonic clock.
    received_s: float
    # In the form the models are fed (see video.py).
    image: np.ndarray
    # When the phone captured it, on this machine's monotonic clock; None before
    # the phone's clock is synced with the service.
    captured_s: float | None = None
    # The JPEG exactly as the camera sent it: full size, not re-encoded.
    jpeg: bytes | None = None

    def age_s(self, now_s: float) -> float:
        """Seconds since the phone took it; since it arrived, before the clocks are synced."""
        return now_s - (self.captured_s if self.captured_s is not None else self.received_s)


@dataclass(frozen=True, slots=True)
class LiveSample:
    status: dict[str, Any]
    frame: LiveFrame | None
    # Seconds since anything arrived from the service.
    silent_s: float


def _split_frame(message: bytes) -> tuple[dict[str, Any], bytes] | None:
    """A model-feed frame as (header, JPEG), or None if it is not one."""
    if len(message) < _LENGTH_PREFIX.size:
        return None
    (length,) = _LENGTH_PREFIX.unpack_from(message)
    end = _LENGTH_PREFIX.size + length
    if end > len(message):
        return None
    try:
        header = json.loads(message[_LENGTH_PREFIX.size : end])
    except ValueError:
        return None
    return (header, message[end:]) if isinstance(header, dict) else None


def _captured_s(header: dict[str, Any], received_s: float) -> float | None:
    age_ms = header.get("age_ms")
    if not isinstance(age_ms, int | float) or isinstance(age_ms, bool) or age_ms < 0:
        return None
    return received_s - age_ms / MS_PER_S


def feed_address(service: str) -> str:
    """``http(s)://host[:port]`` of the driving service as its model feed's address."""
    parts = urlparse(service if "://" in service else f"http://{service}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the service address should look like http://127.0.0.1:8000")
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}/ws/model"


class LiveFeed:
    """Keeps the newest status and the last few seconds of frames from the service."""

    def __init__(self, service: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.service = service
        self.address = feed_address(service)
        self._clock = clock
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {}
        self._frames: deque[LiveFrame] = deque()
        self._count = 0
        self._heard_s: float | None = None
        # How far the chair has turned since the feed began (radians, positive left), and
        # when the gyro last gave a fresh reading.
        self._yaw_rad = 0.0
        self._gyro_s: float | None = None
        # The service's own running angle (degrees) at that reading, if it sends one.
        self._service_yaw: float | None = None
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
                # certifi's CAs: python.org builds on macOS have none, so wss:// would fail.
                tls = ssl.create_default_context(cafile=certifi.where())
                secure = {"ssl": tls} if self.address.startswith("wss://") else {}
                with connect(
                    self.address, open_timeout=5, max_size=8 * 1024 * 1024, **secure
                ) as socket:
                    while not self._stop.is_set():
                        try:
                            message = socket.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        self._take(message)
                        wait_s = 0.5
            except ConnectionClosed:
                self.problem = "the service closed the connection"
            except InvalidStatus as refused:
                self.problem = (
                    f"the service refused the connection ({refused.response.status_code})"
                )
            except (OSError, TimeoutError) as error:
                self.problem = f"cannot reach the service: {error}"
            self._stop.wait(wait_s)
            wait_s = min(wait_s * 2, 5.0)

    def _take(self, message: str | bytes) -> None:
        now = self._clock()
        if isinstance(message, str):
            try:
                status = json.loads(message)
            except ValueError:
                return
            with self._lock:
                self._status, self._heard_s, self.problem = status, now, None
                self._turned(status, now)
            return
        parts = _split_frame(message)
        if parts is None:
            return
        header, jpeg = parts
        try:
            with Image.open(io.BytesIO(jpeg)) as image:
                frame = standardize_frame(np.asarray(image.convert("RGB")))
        except OSError:
            return
        captured = _captured_s(header, now)
        with self._lock:
            self._count += 1
            self._frames.append(LiveFrame(self._count, now, frame, captured, jpeg))
            while self._frames and now - self._frames[0].received_s > HISTORY_S:
                self._frames.popleft()
            self._heard_s = now

    def _turned(self, status: dict[str, Any], now: float) -> None:
        """Add the turn since the last fresh gyro reading to the running angle.

        The service sums the gyro sample by sample on the phone's own clock, with the
        gyro's resting rate taken off (``yaw_deg``, drive_service/motion.py), so only the
        change in that sum is added here: when messages arrive does not matter. After a gap
        the sum may have started again (the phone reconnected), so the first reading only
        sets where to count from.
        """
        motion = status.get("motion") or {}
        rate, yaw = motion.get("yaw_rate_dps"), motion.get("yaw_deg")
        if not motion.get("fresh") or rate is None:
            return
        continuing = self._gyro_s is not None and now - self._gyro_s < GYRO_FRESH_S
        if yaw is not None:
            if continuing and self._service_yaw is not None:
                self._yaw_rad += math.radians(yaw - self._service_yaw)
            self._service_yaw = yaw
        elif continuing and abs(rate) > 0.5:
            # A service from before it sent ``yaw_deg``: one rate per message, timed by
            # arrival, and blind to turns slower than its resting wander. Remove this once
            # the service on the board is updated.
            self._yaw_rad += math.radians(rate) * (now - self._gyro_s)
        self._gyro_s = now

    def yaw_rad(self) -> float:
        """How far the chair has turned since the feed began, positive left."""
        return self._yaw_rad

    def gyro_fresh(self) -> bool:
        return self._gyro_s is not None and self._clock() - self._gyro_s < GYRO_FRESH_S

    def picture_change(self, span_s: float = 0.3) -> float | None:
        """How much the picture has changed over about ``span_s``: the mean difference in
        brightness (0-255) between the newest frame and one that much older, both shrunk.

        Standing still it is about 0.3; driving or turning, 4 or more (measured on the
        chair's runs). None when there are not two frames that far apart to compare.
        """
        with self._lock:
            frames = list(self._frames)
        older = [f for f in frames[:-1] if frames[-1].received_s - f.received_s >= span_s]
        if not older or self._clock() - frames[-1].received_s > MAX_FRAME_AGE_S:
            return None
        new, old = (
            f.image[::8, ::8].astype(np.float32).mean(axis=2) for f in (frames[-1], older[-1])
        )
        return float(np.abs(new - old).mean())

    def fresh_frame(self) -> LiveFrame | None:
        """The newest frame, or None when there is none young enough to drive on."""
        frame = self.latest().frame
        if frame is None or frame.age_s(self._clock()) > MAX_FRAME_AGE_S:
            return None
        return frame

    def latest(self) -> LiveSample:
        with self._lock:
            frame = self._frames[-1] if self._frames else None
            silent = float("inf") if self._heard_s is None else self._clock() - self._heard_s
            return LiveSample(dict(self._status), frame, silent)

    def window(self, count: int, frame_hz: float) -> list[LiveFrame]:
        """The newest ``count`` frames about ``1 / frame_hz`` apart, oldest first.

        Spaced by when each picture was taken, so a stall that delivers several
        frames at once still gives the model evenly spaced views. Where no frame
        was taken at the wanted moment the nearest one stands in, and a short
        history repeats its oldest frame.
        """
        with self._lock:
            frames = list(self._frames)
        if not frames:
            return []
        # Capture and arrival times differ by the link's latency, so never mix
        # them: one frame without a capture time puts the whole window on arrival.
        by_capture = all(f.captured_s is not None for f in frames)
        times = [
            f.captured_s if by_capture and f.captured_s is not None else f.received_s
            for f in frames
        ]
        # Anchored on the newest arrival, the same frame latest() reports.
        newest = times[-1]
        picked = []
        for back in range(count):
            wanted = newest - back / frame_hz
            nearest = min(range(len(frames)), key=lambda i: abs(times[i] - wanted))
            picked.append(frames[nearest])
        return picked[::-1]


def ipad_message(status: dict[str, Any]) -> dict[str, Any]:
    """What the rider has asked for on the iPad, in words for a person to read."""
    goal, destination = status.get("goal"), status.get("destination") or {}
    # ``request`` changes when the rider asks for something new, and only then.
    if goal and goal.get("kind") == "instruction":
        return {
            "kind": "instruction",
            "text": f"“{goal['instruction']}”",
            "request": f"goal-{goal.get('version')}",
        }
    if not goal and not destination:
        return {"kind": None, "text": "Nothing set on the iPad", "request": None}
    away = destination.get("distance_m")
    where = f"{away:.0f} m away" if away is not None else "distance unknown without GPS"
    if goal and goal.get("kind") == "route":
        return {
            "kind": "route",
            "text": f"Map destination, {where}, by a planned route",
            "request": f"goal-{goal.get('version')}",
        }
    return {
        "kind": "destination",
        "text": f"Map destination, {where}, no route",
        "request": f"point-{destination.get('lat')}-{destination.get('lon')}",
    }


# How far along the route the model is aimed.
ROUTE_LOOKAHEAD_M = 10.0
# Closer than this counts as there: a phone's GPS cannot tell much finer.
ARRIVE_M = 3.0
ARRIVED = "arrived at the iPad's destination"


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

        destination = status.get("destination") or {}
        if not goal and not destination:
            return None, "no destination set on the iPad yet"

        # Everything from here needs to know where the chair is and which way it faces.
        placed = gps and gps.get("fresh") and heading.get("reference") == "true"
        if not placed or heading.get("deg") is None:
            return None, "the iPad has a destination, but the chair has no fresh GPS and heading"
        facing = float(heading["deg"])
        distance = destination.get("distance_m")
        if distance is not None and float(distance) <= ARRIVE_M:
            return None, ARRIVED

        if goal and goal.get("kind") == "route":
            try:
                route = self._route_for(int(goal["version"]))
            except (httpx.HTTPError, KeyError, ValueError) as error:
                return None, f"could not fetch the route from the service: {error}"
            if route is not None:
                east, north = to_local(gps["lat"], gps["lon"], route.origin)
                self._progress_m = route.project(east, north, self._progress_m)
                left_m = route.length_m - self._progress_m
                if left_m <= ARRIVE_M:
                    return None, ARRIVED
                target = route.point_at(self._progress_m + ROUTE_LOOKAHEAD_M)
                ahead, left = world_to_chair(target, east, north, facing)[0]
                return Goal(
                    point_xy=(float(ahead), float(left))
                ), f"iPad route: {left_m:.0f} m left"

        # A bare destination: aim straight at it.
        bearing = destination.get("bearing_deg")
        if distance is None or bearing is None:
            return None, "the iPad has a destination, but the chair has no fresh GPS"
        off = math.radians(float(bearing) - facing)
        point = (float(distance) * math.cos(off), -float(distance) * math.sin(off))
        return Goal(point_xy=point), f"iPad destination: {float(distance):.0f} m away, no route"
