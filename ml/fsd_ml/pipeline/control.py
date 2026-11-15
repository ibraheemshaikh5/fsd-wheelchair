"""Turn the chosen path into a speed and a turn rate.

This is the one place a model's path becomes a command. The speed is the model's own
(``speed_limit_mps``, see omnivla.implied_speed), never raised and never ramped here: the
board daemon eases the stick itself, and a stop must be instant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class ControlConfig:
    # The speed for a model that does not give its own.
    cruise_mps: float = 1.0
    # Steer toward the point this far along the path.
    lookahead_m: float = 1.2
    max_yaw_rate_radps: float = 1.0
    # Slows the chair in tight turns so the rider is not thrown sideways.
    max_lateral_accel_mps2: float = 0.6
    # A target this close to straight ahead is driven straight at, so noise in a path does
    # not become steering. It can be small: once the chair is moving, a small sideways stick
    # is a small, smooth turn (5% turns it about 4 degrees a second at 10% forward).
    steer_dead_zone_deg: float = 1.0


def _target(path_xy: np.ndarray, lookahead_m: float) -> np.ndarray:
    distances = np.linalg.norm(path_xy, axis=1)
    beyond = np.nonzero(distances >= lookahead_m)[0]
    return path_xy[beyond[0]] if len(beyond) else path_xy[-1]


def follow(
    path_xy: np.ndarray,
    speed_factor: float,
    config: ControlConfig | None = None,
    speed_limit_mps: float | None = None,
) -> tuple[float, float]:
    """(speed m/s, turn rate rad/s, positive left) to follow a path in the chair's frame.

    ``speed_factor`` is the footprint check's verdict: 1 clear, less when slowing, 0 blocked
    (which stops the chair outright; it does not turn on the spot either).
    """
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

    off_deg = abs(math.degrees(math.atan2(left, ahead)))
    curvature = 0.0 if off_deg < config.steer_dead_zone_deg else 2.0 * left / (distance * distance)
    speed = config.cruise_mps * min(1.0, speed_factor)
    if speed_limit_mps is not None:
        speed = min(speed, max(0.0, speed_limit_mps) * min(1.0, speed_factor))
    if abs(curvature) > 1e-6:
        speed = min(speed, math.sqrt(config.max_lateral_accel_mps2 / abs(curvature)))
        speed = min(speed, config.max_yaw_rate_radps / abs(curvature))
    # The turn rate is in proportion to how far off the target is, at the speed being driven.
    return speed, speed * curvature
