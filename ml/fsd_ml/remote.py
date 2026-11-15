"""Run a driver on another machine and call it over HTTP.

The model sits behind the same ``propose()`` as a local one, so the console does not know
where it runs. The server side is ``info()`` and ``handle()``, served by Modal
(modal_omnivla.py).

Each request carries every frame the model needs, as JPEG, so the server keeps
no state between calls.
"""

from __future__ import annotations

import base64
import io
import os
import time
from typing import Any

import httpx
import numpy as np
from PIL import Image

from fsd_ml.drivers import Candidate, Driver, Goal, Motion

TOKEN_ENV = "FSD_DRIVER_TOKEN"
# Frames are shrunk to this width before sending; the models take less.
SEND_WIDTH = 384
JPEG_QUALITY = 80
# A decision that takes longer than this is too old to drive on; the call is given up.
DECISION_TIMEOUT_S = 1.5


def _encode(frame: np.ndarray) -> str:
    image = Image.fromarray(frame)
    if image.width > SEND_WIDTH:
        image = image.resize((SEND_WIDTH, round(image.height * SEND_WIDTH / image.width)))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _decode(data: str) -> np.ndarray:
    with Image.open(io.BytesIO(base64.b64decode(data))) as image:
        return np.asarray(image.convert("RGB"))


def info(driver: Driver) -> dict[str, Any]:
    return {
        "name": driver.name,
        "context_frames": driver.context_frames,
        "frame_hz": driver.frame_hz,
        "decision_hz": getattr(driver, "decision_hz", driver.frame_hz),
        "provider": getattr(driver, "provider", None),
    }


def handle(driver: Driver, payload: dict[str, Any]) -> dict[str, Any]:
    """Server side: one request body in, one response body out."""
    frames = [_decode(frame) for frame in payload["frames"]]
    goal = payload.get("goal") or {}
    point = goal.get("point_xy")
    motion = payload.get("motion") or {}
    started = time.perf_counter()
    photo = goal.get("image")
    candidates = driver.propose(
        frames,
        Goal(
            tuple(point) if point else None,
            goal.get("instruction"),
            _decode(photo) if photo else None,
        ),
        Motion(motion.get("speed_mps"), motion.get("yaw_rate_radps")),
    )
    return {
        "driver": driver.name,
        "inference_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "candidates": [
            {
                "score": c.score,
                "speed_mps": c.speed_mps,
                "path_xy": np.asarray(c.path_xy).round(3).tolist(),
            }
            for c in candidates
        ],
    }


def authorized(header: str | None) -> bool:
    """With FSD_DRIVER_TOKEN set on the server, requests must carry it as a bearer token."""
    token = os.environ.get(TOKEN_ENV)
    return not token or header == f"Bearer {token}"


class RemoteDriver:
    """A driver served at ``url``. Keeps one connection open between calls."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        timeout_s: float = DECISION_TIMEOUT_S,
        start_timeout_s: float = 1800.0,
    ) -> None:
        token = token or os.environ.get(TOKEN_ENV)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # Redirects are followed because a hosted endpoint answers a slow request with one.
        self._client = httpx.Client(
            base_url=url.rstrip("/"), headers=headers, timeout=timeout_s, follow_redirects=True
        )
        # The first call may have to wait for the machine to start and load the model.
        # Reaching the address at all should be quick, so a wrong one fails fast.
        waiting = httpx.Timeout(start_timeout_s, connect=10.0)
        described = self._client.get("/info", timeout=waiting).raise_for_status().json()
        self.host = self._client.base_url.host
        self.name = f"remote-{described['name']}"
        self.context_frames = int(described["context_frames"])
        self.frame_hz = float(described["frame_hz"])
        self.decision_hz = float(described.get("decision_hz", self.frame_hz))
        self.provider = described.get("provider")
        # Timing of the latest call: there and back, and the model's share of it.
        self.round_trip_ms: float | None = None
        self.inference_ms: float | None = None

    def propose(self, frames: list[np.ndarray], goal: Goal, motion: Motion) -> list[Candidate]:
        body = {
            "frames": [_encode(frame) for frame in frames],
            "goal": {
                "point_xy": goal.point_xy,
                "instruction": goal.instruction,
                "image": None if goal.image is None else _encode(goal.image),
            },
            "motion": {"speed_mps": motion.speed_mps, "yaw_rate_radps": motion.yaw_rate_radps},
        }
        started = time.perf_counter()
        answer = self._client.post("/propose", json=body).raise_for_status().json()
        self.round_trip_ms = (time.perf_counter() - started) * 1000.0
        self.inference_ms = answer["inference_ms"]
        return [
            Candidate(np.array(c["path_xy"]), c["score"], c.get("speed_mps"))
            for c in answer["candidates"]
        ]

    def close(self) -> None:
        self._client.close()
