"""Turn the chosen path into a speed and a turn rate (pipeline stage 8)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class ControlConfig:
    cruise_mps: float = 1.0
    # Steer toward the point this far along the path.
    lookahead_m: float = 1.2
    max_yaw_rate_radps: float = 1.0
    max_accel_mps2: float = 0.8
    max_brake_mps2: float = 2.0
    # Slows the chair in tight turns so the rider is not thrown sideways.
    max_lateral_accel_mps2: float = 0.6


def _target(path_xy: np.ndarray, lookahead_m: float) -> np.ndarray:
    distances = np.linalg.norm(path_xy, axis=1)
    beyond = np.nonzero(distances >= lookahead_m)[0]
    return path_xy[beyond[0]] if len(beyond) else path_xy[-1]


def follow(
    path_xy: np.ndarray,
    speed_factor: float,
    current_speed_mps: float,
    dt_s: float,
    config: ControlConfig | None = None,
    speed_limit_mps: float | None = None,
) -> tuple[float, float]:
    """(speed m/s, turn rate rad/s, positive left) to follow a path in the chair's frame."""
    config = config or ControlConfig()
    path = np.asarray(path_xy, dtype=np.float64).reshape(-1, 2)
    if len(path) == 0 or speed_factor <= 0.0:
        return 0.0, 0.0

    ahead, left = _target(path, config.lookahead_m)
    distance = math.hypot(ahead, left)
    if distance < 1e-6:
        return 0.0, 0.0

    if ahead <= 0.1 * distance:
        # The path starts beside or behind the chair: turn on the spot to face it.
        return 0.0, math.copysign(config.max_yaw_rate_radps * 0.6, left)

    curvature = 2.0 * left / (distance * distance)
    wanted = config.cruise_mps * min(1.0, speed_factor)
    if speed_limit_mps is not None:
        # A model that plans its own speed may be slowing for something we cannot see.
        wanted = min(wanted, max(0.0, speed_limit_mps))
    if abs(curvature) > 1e-6:
        wanted = min(wanted, math.sqrt(config.max_lateral_accel_mps2 / abs(curvature)))
        wanted = min(wanted, config.max_yaw_rate_radps / abs(curvature))

    low = current_speed_mps - config.max_brake_mps2 * dt_s
    high = current_speed_mps + config.max_accel_mps2 * dt_s
    speed = max(0.0, min(high, max(low, wanted)))
    turn = max(-config.max_yaw_rate_radps, min(config.max_yaw_rate_radps, speed * curvature))
    return speed, turn
