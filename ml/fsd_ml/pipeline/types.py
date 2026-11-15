"""What the footprint check is told about the things in the chair's way."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
