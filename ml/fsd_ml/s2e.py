"""S2E (UCLA, ICLR 2026) as a driver: 11 camera frames and a target point in, one path out.

This runs the model-zoo ONNX file directly. The preprocessing and scaling below
follow the zoo's own ``S2E/inference.py``; that file is not imported, which keeps
PyTorch and OpenCV out of the install.

    uv run --group weights python -m fsd_ml.weights s2e
    uv run --group onnx python -m fsd_ml.sim.run clear --driver s2e
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image

from fsd_ml.drivers import Candidate, Goal, Motion
from fsd_ml.weights import WEIGHTS_DIR

ONNX_PATH = WEIGHTS_DIR / "nav-zoo" / "S2E" / "s2e.onnx"
IMAGE_SIZE = 256
# The model's waypoints times this are metres.
WAYPOINT_SCALE = 0.25
DEFAULT_GOAL_XY = (5.0, 0.0)


def goal_input(point_xy: tuple[float, float]) -> np.ndarray:
    """(ahead, left) metres as the model's [distance / 200, cos, sin]."""
    ahead, left = point_xy
    distance = max(min(math.hypot(ahead, left), 200.0), 0.1) / 200.0
    angle = math.atan2(left, ahead)
    return np.array([[distance, math.cos(angle), math.sin(angle)]], dtype=np.float32)


def prepare(frames: list[np.ndarray]) -> np.ndarray:
    """RGB uint8 frames as (1, n, 3, 256, 256) float32 in [0, 1]."""
    resized = [
        np.asarray(Image.fromarray(frame).resize((IMAGE_SIZE, IMAGE_SIZE), Image.BILINEAR))
        for frame in frames
    ]
    stacked = np.stack(resized).astype(np.float32) / 255.0
    return stacked.transpose(0, 3, 1, 2)[np.newaxis]


class S2EDriver:
    name = "s2e"
    context_frames = 11
    # The zoo does not state S2E's frame rate; its neighbours use about 5 Hz.
    frame_hz = 5.0

    def __init__(self, onnx_path: Path = ONNX_PATH) -> None:
        if not onnx_path.is_file():
            raise FileNotFoundError(
                f"{onnx_path} is missing; run: uv run --group weights python -m fsd_ml.weights s2e"
            )
        import onnxruntime

        onnxruntime.set_default_logger_severity(3)
        available = onnxruntime.get_available_providers()
        wanted = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in available]
        self._session = onnxruntime.InferenceSession(str(onnx_path), providers=wanted)
        self.provider = self._session.get_providers()[0]

    def propose(self, frames: list[np.ndarray], goal: Goal, motion: Motion) -> list[Candidate]:
        outputs = self._session.run(
            None,
            {
                "obs_images": prepare(frames),
                "goal": goal_input(goal.point_xy or DEFAULT_GOAL_XY),
            },
        )
        path = outputs[0][0, :, :2].astype(np.float64) * WAYPOINT_SCALE
        return [Candidate(path, score=1.0)]
