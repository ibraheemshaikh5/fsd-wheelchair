"""The one interface every driver model sits behind.

A driver looks at recent camera frames and proposes where to drive. It only
proposes: whether the chair fits, and how fast it goes, is decided elsewhere
(docs/driving-pipeline.md).

Paths are in the chair's frame, in metres: x ahead, y to the left.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True, slots=True)
class Goal:
    """What the driver is asked to reach: a point, an instruction, a photo, or a mix.

    Most models read only the point. OmniVLA reads all three.
    """

    # Target relative to the chair, metres (x ahead, y left).
    point_xy: tuple[float, float] | None = None
    instruction: str | None = None
    # A photo of the destination, RGB uint8.
    image: np.ndarray | None = None


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
    # The speed the model wants along this path, for models that say. The chair
    # never goes faster than this.
    speed_mps: float | None = None


class Driver(Protocol):
    name: str
    # How many recent frames the model needs per step.
    context_frames: int
    # How far apart in time those frames should be, as a rate.
    frame_hz: float

    def propose(self, frames: list[np.ndarray], goal: Goal, motion: Motion) -> list[Candidate]:
        """Candidate paths, best first. ``frames`` are RGB uint8, oldest first."""
        ...


class StraightDriver:
    """Always proposes driving straight ahead. A floor for the bake-off, not a model."""

    name = "straight"
    context_frames = 1
    frame_hz = 20.0

    def __init__(self, distance_m: float = 4.0, points: int = 20) -> None:
        self._path = np.column_stack(
            [np.linspace(distance_m / points, distance_m, points), np.zeros(points)]
        )

    def propose(self, frames: list[np.ndarray], goal: Goal, motion: Motion) -> list[Candidate]:
        return [Candidate(path_xy=self._path.copy(), score=1.0)]


class FanDriver:
    """Heads for the goal point, and offers detours to either side.

    It ignores the camera, so it stands in for a model in the simulator: it gives
    the rest of the pipeline several scored paths to choose between.
    """

    name = "fan"
    context_frames = 1
    frame_hz = 20.0

    def __init__(
        self,
        reach_m: float = 6.0,
        offsets_m: tuple[float, ...] = (0.0, 0.7, -0.7, 1.4, -1.4),
        points: int = 24,
    ) -> None:
        self._reach_m = reach_m
        self._offsets_m = offsets_m
        self._points = points

    def propose(self, frames: list[np.ndarray], goal: Goal, motion: Motion) -> list[Candidate]:
        target = np.array(goal.point_xy if goal.point_xy else (self._reach_m, 0.0), dtype=float)
        distance = float(np.linalg.norm(target))
        if distance < 1e-6:
            return []
        if distance > self._reach_m:
            target *= self._reach_m / distance
            distance = self._reach_m
        along = np.linspace(1.0 / self._points, 1.0, self._points)
        sideways = np.array([-target[1], target[0]]) / distance
        candidates = []
        for offset in self._offsets_m:
            # A bulge to one side that returns to the line at the target.
            bulge = offset * np.sin(np.pi * along)
            path = np.outer(along, target) + np.outer(bulge, sideways)
            candidates.append(Candidate(path, score=1.0 - 0.1 * abs(offset)))
        return candidates


def _s2e() -> Driver:
    from fsd_ml.s2e import S2EDriver

    return S2EDriver()


def _flowpilot() -> Driver:
    from fsd_ml.flowpilot import FlowPilotDriver

    return FlowPilotDriver()


# Built on demand, so a model's runtime is only imported when that model is used.
DRIVERS: dict[str, Callable[[], Driver]] = {
    StraightDriver.name: StraightDriver,
    FanDriver.name: FanDriver,
    "s2e": _s2e,
    "flowpilot": _flowpilot,
}


def make_driver(name: str) -> Driver:
    """A driver by name, or ``remote:<url>`` for one served over HTTP (see remote.py)."""
    if name.startswith("remote:"):
        from fsd_ml.remote import RemoteDriver

        return RemoteDriver(name.removeprefix("remote:"))
    if name not in DRIVERS:
        raise KeyError(f"unknown driver {name!r}; have {', '.join(sorted(DRIVERS))}")
    return DRIVERS[name]()
