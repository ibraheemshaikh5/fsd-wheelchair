"""Pick a path the chair physically fits along.

The driver model only proposes paths. This sweeps the chair's rectangle along
each one and takes the best-scored path that touches nothing. It is plain
geometry, so it holds whichever model is driving.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from fsd_ml.chair import Body
from fsd_ml.drivers import Candidate
from fsd_ml.pipeline.types import Obstacle, PathStatus

SAMPLE_SPACING_M = 0.1
# How far short of touching something (with its margin) a run stops: next to nothing when
# barely moving, and more the faster it goes (about 0.26 m at the 0.45 m/s creep, 0.41 m at
# 0.75 m/s). The time covers noticing it (three looks in a row) and coasting to a stop. A
# chair creeping up to something therefore ends about a hand's width from it.
STOP_STILL_M = 0.03
STOP_REACT_S = 0.5
# The clearance kept from something the chair is already nearer to than its margin.
BESIDE_M = 0.05
# Something the chair already stands against stops it only where it would come this much
# nearer still.
CLOSING_M = 0.03


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


def _resample(path_xy: np.ndarray, from_chair: bool = True) -> np.ndarray:
    """The path, from the chair's centre unless it is said to be on it, as evenly spaced points."""
    points = np.asarray(path_xy, dtype=np.float64).reshape(-1, 2)
    if from_chair:
        points = np.vstack([[0.0, 0.0], points])
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
    path_xy: np.ndarray,
    obstacles: Sequence[Obstacle],
    shape: Body,
    extra_m: float = 0.0,
    from_chair: bool = True,
    closing: bool = False,
) -> tuple[float, Obstacle] | None:
    """Distance along the path at which the chair first touches an obstacle, and which one.

    With ``closing``, an obstacle only counts where the chair has come CLOSING_M further
    into it than it is at the path's start: for things it already stands against.

    The path is taken to start at the chair's centre. For a chair already driving it,
    ``from_chair`` false takes the path as given, from the point of it the chair is at:
    joining the chair to the next point would turn every few centimetres it is off the
    line into a sharp bend, and the chair's body with it.
    """
    if not obstacles:
        return None
    samples = _resample(path_xy, from_chair)
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
        # How far inside the chair's box (with its margin) the obstacle is: negative, outside.
        inside = np.minimum(
            half_length + obstacle.radius_m - np.abs(ahead),
            half_width + obstacle.radius_m - np.abs(left),
        )
        touching = inside >= 0
        if closing:
            touching &= inside > max(float(inside[0]), 0.0) + CLOSING_M
        if touching.any():
            distance = float(np.argmax(touching)) * SAMPLE_SPACING_M
            if best is None or distance < best[0]:
                best = (distance, obstacle)
    return best


def beside(obstacles: Sequence[Obstacle], shape: Body) -> list[Obstacle]:
    """The obstacles already nearer the chair, as it stands, than its margin."""
    here = np.array([[SAMPLE_SPACING_M, 0.0]])
    return [obstacle for obstacle in obstacles if first_hit(here, [obstacle], shape)]


def first_hit_from_here(
    path_xy: np.ndarray, obstacles: Sequence[Obstacle], shape: Body, from_chair: bool = True
) -> tuple[float, Obstacle] | None:
    """As ``first_hit``, for a path that starts where the chair stands now.

    The margin is room the chair keeps from things it comes up to. Something it is already
    nearer to than that (it stands beside it) cannot be kept that far away, and holding the
    chair to it would leave it nowhere to go: such a thing need only be cleared by BESIDE_M,
    and if the chair is already nearer than that too (it stands against it, or the reading
    of where it is, is a little off), it stops the chair only where the path would bring
    the body nearer still. So the chair can always drive along or away from what is beside
    it, and never toward it.
    """
    near = beside(obstacles, shape)
    known = {id(obstacle) for obstacle in near}
    far = [obstacle for obstacle in obstacles if id(obstacle) not in known]
    close = Body(shape.width_m, shape.length_m, min(shape.margin_m, BESIDE_M))
    hits = [
        hit
        for hit in (
            first_hit(path_xy, far, shape, from_chair=from_chair),
            first_hit(path_xy, near, close, from_chair=from_chair, closing=True),
        )
        if hit
    ]
    return min(hits, key=lambda hit: hit[0]) if hits else None


def turn_room_deg(degrees_left: float, obstacles: Sequence[Obstacle], shape: Body) -> float:
    """How far of a turn on the spot the chair can make before its body touches something.

    The chair turns about the middle of its drive wheels, behind its own middle, so its
    front swings wide. Returns the whole turn if nothing is in the way, else the part of it
    (same sign, in 5 degree steps) that is clear by BESIDE_M. What already overlaps the
    body as it stands is left out: that is a wrong reading, or it is too late.
    """
    if not obstacles or degrees_left == 0:
        return degrees_left
    pivot = np.array([-shape.pivot_behind_m, 0.0])
    where = np.array([[o.x, o.y] for o in obstacles]) - pivot
    reach = np.array([o.radius_m for o in obstacles]) + BESIDE_M
    half_length, half_width = shape.length_m / 2 + reach, shape.width_m / 2 + reach

    def touching(turned_deg: float) -> np.ndarray:
        turned = math.radians(turned_deg)
        ahead = where[:, 0] * math.cos(turned) + where[:, 1] * math.sin(turned) + pivot[0]
        left = -where[:, 0] * math.sin(turned) + where[:, 1] * math.cos(turned)
        return (np.abs(ahead) <= half_length) & (np.abs(left) <= half_width)

    judged = ~touching(0.0)
    way, clear = math.copysign(1.0, degrees_left), 0.0
    for step in np.arange(5.0, abs(degrees_left) + 5.0, 5.0):
        angle = min(step, abs(degrees_left))
        if (touching(way * angle) & judged).any():
            break
        clear = angle
    return way * clear


def stop_distance_m(speed_mps: float) -> float:
    """How much of its way must be left clear in front of a chair going this fast.

    The one rule for a bench or pilot run: paths are offered no closer than this to what
    they meet (fsd_ml/candidates.py), and a run stops when less than this is left
    (Session.blocked), so a path that was offered is not then cut off by a stricter rule.
    """
    return STOP_STILL_M + STOP_REACT_S * max(0.0, speed_mps)


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
