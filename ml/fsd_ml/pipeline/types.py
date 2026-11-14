"""What goes into one pass of the pipeline, and what comes out.

The outputs follow docs/outputs.md. Times are milliseconds on one clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

Mode = Literal["idle", "ready", "driving", "arrived", "manual"]
PathStatus = Literal["clear", "slowing", "blocked"]
ObstacleKind = Literal["pedestrian", "vehicle", "object"]


@dataclass(frozen=True, slots=True)
class Obstacle:
    """Something in the way, in the chair's frame (metres ahead, metres left)."""

    id: str
    kind: ObstacleKind
    x: float
    y: float
    radius_m: float = 0.3


@dataclass(frozen=True, slots=True)
class Sensors:
    """Everything the phone and the obstacle stage know at time ``t_ms``."""

    t_ms: float
    # Newest camera frame (RGB uint8) and when it was captured.
    frame: np.ndarray | None = None
    frame_t_ms: float | None = None
    lat: float | None = None
    lon: float | None = None
    fix_t_ms: float | None = None
    heading_deg: float | None = None
    speed_mps: float | None = None
    # Positive turns left.
    yaw_rate_radps: float | None = None
    obstacles: tuple[Obstacle, ...] = ()


@dataclass(frozen=True, slots=True)
class ChairStatus:
    """What the chair reports back (docs/outputs.md, "chair" message)."""

    mode: Literal["auto", "manual"] = "auto"
    joystick_active: bool = False
    estop: bool = False
    battery_pct: float | None = None
    applied_seq: int | None = None


@dataclass(frozen=True, slots=True)
class DriveCommand:
    seq: int
    t_ms: float
    v_mps: float
    # Positive turns left.
    w_radps: float
    ttl_ms: float


@dataclass(frozen=True, slots=True)
class Pose:
    """Where the chair is, in world metres from the trip's origin."""

    east: float
    north: float
    heading_deg: float
    speed_mps: float
    yaw_rate_radps: float


@dataclass(frozen=True, slots=True)
class Decision:
    """The result of one pass: the command, and everything that explains it."""

    command: DriveCommand
    mode: Mode
    path_status: PathStatus
    # Chosen path in the chair's frame, shape (n, 2). Empty when there is none.
    path_xy: np.ndarray
    obstacles: tuple[Obstacle, ...]
    yielding_to_id: str | None
    reason: str | None
    take_over: str | None
    engage_blocked: str | None
    pose: Pose | None
    progress_m: float
    remaining_m: float
    # Why the gate refused to move the chair this pass, if it did.
    gate_reason: str | None = None
    model_name: str | None = None
    model_age_ms: float | None = None
    inference_ms: float | None = None
    # Every path the model proposed, in the chair's frame, with its score.
    candidates: tuple[tuple[float, np.ndarray], ...] = field(default=())
    chosen_index: int | None = None
