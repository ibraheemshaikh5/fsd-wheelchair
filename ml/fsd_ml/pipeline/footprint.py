"""Pick a path the chair physically fits along.

The driver model only proposes paths. This sweeps the chair's rectangle along
each one and takes the best-scored path that touches nothing. It is plain
geometry, so it holds whichever model is driving.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from fsd_ml.chair import Body
from fsd_ml.drivers import Candidate
from fsd_ml.pipeline.types import Obstacle, PathStatus

SAMPLE_SPACING_M = 0.1


@dataclass(frozen=True, slots=True)
class FootprintConfig:
    # The chair's size and the space kept clear round it (ml/chair.json).
    shape: Body = Body()
    # An obstacle this much outside the margin slows the chair without blocking it.
    caution_m: float = 0.4
    # Obstacles further along the path than this do not change the speed yet.
    slow_distance_m: float = 4.0
    # With no clear path, keep creeping while the nearest hit is further than this.
    stop_distance_m: float = 1.5
    slowing_factor: float = 0.5
    # Slowest speed factor while closing on something that blocks every path.
    creep_factor: float = 0.3


@dataclass(frozen=True, slots=True)
class Choice:
    index: int | None
    path_xy: np.ndarray
    status: PathStatus
    # Multiplies the cruise speed: 1 clear, less when slowing, 0 blocked.
    speed_factor: float
    yielding_to_id: str | None
    reason: str | None
    # The speed the model wanted along this path, if it said.
    speed_limit_mps: float | None = None


def _resample(path_xy: np.ndarray) -> np.ndarray:
    """The path from the chair's centre, as evenly spaced points."""
    points = np.vstack([[0.0, 0.0], np.asarray(path_xy, dtype=np.float64).reshape(-1, 2)])
    legs = np.linalg.norm(np.diff(points, axis=0), axis=1)
    keep = np.concatenate([[True], legs > 1e-9])
    points = points[keep]
    along = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))])
    if along[-1] < SAMPLE_SPACING_M:
        return points
    targets = np.arange(0.0, along[-1] + 1e-9, SAMPLE_SPACING_M)
    return np.column_stack(
        [np.interp(targets, along, points[:, 0]), np.interp(targets, along, points[:, 1])]
    )


def first_hit(
    path_xy: np.ndarray, obstacles: Sequence[Obstacle], shape: Body, extra_m: float = 0.0
) -> tuple[float, Obstacle] | None:
    """Distance along the path at which the chair first touches an obstacle, and which one."""
    if not obstacles:
        return None
    samples = _resample(path_xy)
    if len(samples) < 2:
        return None
    directions = np.gradient(samples, axis=0)
    headings = np.arctan2(directions[:, 1], directions[:, 0])
    half_length = shape.length_m / 2 + shape.margin_m + extra_m
    half_width = shape.width_m / 2 + shape.margin_m + extra_m

    best: tuple[float, Obstacle] | None = None
    for obstacle in obstacles:
        dx = obstacle.x - samples[:, 0]
        dy = obstacle.y - samples[:, 1]
        # The obstacle as seen from the chair at each sample.
        ahead = dx * np.cos(headings) + dy * np.sin(headings)
        left = -dx * np.sin(headings) + dy * np.cos(headings)
        touching = (np.abs(ahead) <= half_length + obstacle.radius_m) & (
            np.abs(left) <= half_width + obstacle.radius_m
        )
        if touching.any():
            distance = float(np.argmax(touching)) * SAMPLE_SPACING_M
            if best is None or distance < best[0]:
                best = (distance, obstacle)
    return best


def _waiting_reason(obstacle: Obstacle, verb: str) -> str:
    noun = {"pedestrian": "pedestrian", "vehicle": "vehicle", "object": "obstacle"}[obstacle.kind]
    return f"{verb} for {noun}"


def choose(
    candidates: Sequence[Candidate],
    obstacles: Sequence[Obstacle],
    config: FootprintConfig | None = None,
) -> Choice:
    """The best-scored candidate the chair fits along; failing that, slow down or stop.

    Only what is within ``slow_distance_m`` changes the chair's speed. A path that
    meets something further off is still driven at full speed for now; it only
    loses to a path that is clear all the way.
    """
    config = config or FootprintConfig()
    if not candidates:
        return Choice(None, np.empty((0, 2)), "blocked", 0.0, None, "No path from the driver model")

    ranked = sorted(range(len(candidates)), key=lambda i: candidates[i].score, reverse=True)
    hits = {i: first_hit(candidates[i].path_xy, obstacles, config.shape) for i in ranked}
    nears = {
        i: first_hit(candidates[i].path_xy, obstacles, config.shape, config.caution_m)
        for i in ranked
        if hits[i] is None
    }

    def pick(index: int, status: PathStatus, factor: float, obstacle: Obstacle | None) -> Choice:
        if obstacle is not None:
            reason = _waiting_reason(obstacle, "Waiting" if status == "blocked" else "Slowing")
        else:
            reason = "Going around obstacle" if index != ranked[0] else None
        return Choice(
            index,
            candidates[index].path_xy,
            status,
            factor,
            obstacle.id if obstacle else None,
            reason,
            candidates[index].speed_mps,
        )

    # 1. Nothing on or beside the path, all the way along.
    for index in ranked:
        if hits[index] is None and nears[index] is None:
            return pick(index, "clear", 1.0, None)

    # 2. Nothing on or beside the path for as far as matters yet. Staying with the
    # best-scored one here keeps the chair from switching paths over something distant.
    for index in ranked:
        found = hits[index] or nears[index]
        if found[0] > config.slow_distance_m:
            return pick(index, "clear", 1.0, None)

    # 3. The chair fits, but passes close to something: go slowly.
    for index in ranked:
        if hits[index] is None:
            return pick(index, "slowing", config.slowing_factor, nears[index][1])

    # 4. Every path meets something close. Creep up to it, then stop.
    index = max(ranked, key=lambda i: hits[i][0])
    distance, obstacle = hits[index]
    if distance <= config.stop_distance_m:
        return pick(index, "blocked", 0.0, obstacle)
    room = (distance - config.stop_distance_m) / (config.slow_distance_m - config.stop_distance_m)
    return pick(index, "slowing", max(config.creep_factor, min(1.0, room)), obstacle)
