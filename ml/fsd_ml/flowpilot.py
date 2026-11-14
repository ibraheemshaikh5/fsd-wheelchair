"""FlowPilot (UCLA, CoRL 2026) as a driver: the small variant, ``flowpilot-dst-small``.

20 camera frames at 20 Hz, a target point and the chair's speed in; six ranked
plans out. This runs the published ONNX graph directly, following its ONNX
guide (github.com/VAIL-UCLA/visnavkit, docs/flowpilot_dst_onnx.md).

    uv run --group weights python -m fsd_ml.weights flowpilot
    uv run --group onnx python -m fsd_ml.viewer video.mp4 --driver flowpilot

Things the model assumes that our setup does not yet match:

- A fisheye camera about 137 degrees wide, 0.456 m above the ground, 16:9.
  Other cameras shift its plans.
- A goal at least 20 m away means "keep driving"; a nearer one means "arriving,
  slow down". Nearer goals are pushed out to 20 m here, since the pipeline
  decides arrival itself.
- A route drawn as a map patch. None is given here, only the target point.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image

from fsd_ml.drivers import Candidate, Goal, Motion
from fsd_ml.weights import WEIGHTS_DIR

ONNX_PATH = WEIGHTS_DIR / "flowpilot" / "flowpilot-dst-small" / "flowpilot_dst_fastvit_t12.onnx"
FRAMES = 20
FRAME_DT_S = 0.05
IMAGE_W, IMAGE_H = 384, 216
PATCH_PX = 80
KEEP_DRIVING_M = 20.0
DEFAULT_GOAL_XY = (KEEP_DRIVING_M, 0.0)
# Per-step [dx, dy, dyaw, v, w] limits the published model was trained with, [low; high].
# Used when the file that ships beside the model is missing.
TRAINING_ACTION_BOUNDS = ((-0.0, -0.09, -0.05, 0.0, -0.85), (0.14, 0.09, 0.05, 2.76, 0.85))


def prepare(frames: list[np.ndarray]) -> np.ndarray:
    """RGB uint8 frames, oldest first, as (1, 20, 3, 216, 384) float32 in [0, 1].

    A shorter history repeats its oldest frame at the front.
    """
    frames = frames[-FRAMES:]
    frames = [frames[0]] * (FRAMES - len(frames)) + list(frames)
    resized = [
        np.asarray(Image.fromarray(f).resize((IMAGE_W, IMAGE_H), Image.Resampling.BOX))
        for f in frames
    ]
    return (np.stack(resized).astype(np.float32) / 255.0).transpose(0, 3, 1, 2)[np.newaxis]


def goal_slots(goal_xy: tuple[float, float], speed_mps: float, yaw_rate_radps: float) -> np.ndarray:
    """The goal as each of the 20 frames saw it: (1, 20, 3) of [distance, cos, sin].

    Assumes the chair held this speed and turn rate over the last second.
    """
    slots = np.empty((FRAMES, 3), dtype=np.float32)
    for slot in range(FRAMES):
        ago = (FRAMES - 1 - slot) * FRAME_DT_S
        # Where the chair was `ago` seconds back, in today's frame.
        yaw = -yaw_rate_radps * ago
        if abs(yaw_rate_radps) < 1e-6:
            x, y = -speed_mps * ago, 0.0
        else:
            radius = speed_mps / yaw_rate_radps
            x, y = -radius * math.sin(yaw_rate_radps * ago), radius * (math.cos(yaw) - 1.0)
        dx, dy = goal_xy[0] - x, goal_xy[1] - y
        ahead = dx * math.cos(yaw) + dy * math.sin(yaw)
        left = -dx * math.sin(yaw) + dy * math.cos(yaw)
        distance = math.hypot(ahead, left)
        slots[slot] = (
            (distance, ahead / distance, left / distance) if distance > 1e-6 else (0.0, 1.0, 0.0)
        )
    return slots[np.newaxis]


def keep_driving(goal_xy: tuple[float, float]) -> tuple[float, float]:
    """The same direction, at least 20 m away."""
    distance = math.hypot(*goal_xy)
    if distance < 1e-6:
        return DEFAULT_GOAL_XY
    scale = max(1.0, KEEP_DRIVING_M / distance)
    return goal_xy[0] * scale, goal_xy[1] * scale


class FlowPilotDriver:
    name = "flowpilot"
    context_frames = FRAMES
    frame_hz = 20.0
    # One call encodes all 20 frames (about 400 ms on a Mac CPU), so on a CPU it
    # cannot decide on every frame.
    decision_hz = 2.0

    def __init__(self, onnx_path: Path = ONNX_PATH) -> None:
        if not onnx_path.is_file():
            raise FileNotFoundError(
                f"{onnx_path} is missing; run: "
                "uv run --group weights python -m fsd_ml.weights flowpilot"
            )
        import onnxruntime

        onnxruntime.set_default_logger_severity(3)
        available = onnxruntime.get_available_providers()
        wanted = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in available]
        self._session = onnxruntime.InferenceSession(str(onnx_path), providers=wanted)
        self.provider = self._session.get_providers()[0]
        if self.provider != "CPUExecutionProvider":
            self.decision_hz = 20.0

        bounds = TRAINING_ACTION_BOUNDS
        sidecar = onnx_path.with_name(onnx_path.stem + ".inputs.npz")
        if sidecar.is_file():
            with np.load(sidecar) as inputs:
                bounds = inputs["action_bounds"]
        self._action_bounds = np.asarray(bounds, dtype=np.float32).reshape(1, 2, 5)
        self._no_route = np.zeros((1, FRAMES, PATCH_PX, PATCH_PX), dtype=np.float32)

    def propose(self, frames: list[np.ndarray], goal: Goal, motion: Motion) -> list[Candidate]:
        speed = motion.speed_mps or 0.0
        turn = motion.yaw_rate_radps or 0.0
        modes, probs = self._session.run(
            ["modes", "probs"],
            {
                "vision": prepare(frames),
                "route_patch": self._no_route,
                "goal": goal_slots(keep_driving(goal.point_xy or DEFAULT_GOAL_XY), speed, turn),
                "ego": np.tile(np.array([speed, turn], dtype=np.float32), (1, FRAMES, 1)),
                "action_bounds": self._action_bounds,
            },
        )
        order = np.argsort(-probs[0], kind="stable")
        return [
            Candidate(
                modes[0, k, :, :2].astype(np.float64),
                score=float(probs[0, k]),
                # The speed the model itself wants over the next second.
                speed_mps=float(modes[0, k, :20, 3].mean()),
            )
            for k in order
        ]
