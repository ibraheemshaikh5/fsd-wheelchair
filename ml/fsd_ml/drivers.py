"""The one interface every driver model sits behind.

A driver looks at recent camera frames and proposes where to drive. It only
proposes: whether the chair fits, and how fast it goes, is decided elsewhere
(docs/driving-pipeline.md).

Paths are in the chair's frame, in metres: x ahead, y to the left.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True, slots=True)
class Goal:
    """What the driver is asked to reach. Outdoors a point; indoors an instruction."""

    # Target relative to the chair, metres (x ahead, y left). None indoors.
    point_xy: tuple[float, float] | None = None
    instruction: str | None = None


@dataclass(frozen=True, slots=True)
class Motion:
    """How the chair is moving now. None where the recording has no source for it."""

    speed_mps: float | None = None
    # Positive turns left.
    yaw_rate_radps: float | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    # Shape (points, 2): x ahead, y left, metres.
    path_xy: np.ndarray
    # Higher is better. Only comparable between candidates from the same call.
    score: float


class Driver(Protocol):
    name: str
    # How many recent frames the model needs per step.
    context_frames: int

    def propose(self, frames: list[np.ndarray], goal: Goal, motion: Motion) -> list[Candidate]:
        """Candidate paths, best first. ``frames`` are RGB uint8, oldest first."""
        ...


class StraightDriver:
    """Always proposes driving straight ahead. A floor for the bake-off, not a model."""

    name = "straight"
    context_frames = 1

    def __init__(self, distance_m: float = 4.0, points: int = 20) -> None:
        self._path = np.column_stack(
            [np.linspace(distance_m / points, distance_m, points), np.zeros(points)]
        )

    def propose(self, frames: list[np.ndarray], goal: Goal, motion: Motion) -> list[Candidate]:
        return [Candidate(path_xy=self._path.copy(), score=1.0)]


# Real models are added here as their drivers are written.
DRIVERS: dict[str, type[Driver]] = {
    StraightDriver.name: StraightDriver,
}


def make_driver(name: str) -> Driver:
    if name not in DRIVERS:
        raise KeyError(f"unknown driver {name!r}; have {', '.join(sorted(DRIVERS))}")
    return DRIVERS[name]()
