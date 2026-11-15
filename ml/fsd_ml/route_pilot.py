"""Gemini drives the chair by drawing its route on the camera's picture.

A model that sees is poor at metres and degrees, and at picking among ready-made moves one
small step at a time; it is good at pointing. So each look it is shown the picture and
draws the way the chair should go as a few points on the floor and marks the goal, or
chooses to look round, or says it has arrived. That is all it decides: where to go, how
close, and when it is there. It is shown a clean picture and told only what the chair did
and where its goal is.

Getting there is the harness's work, done quietly:

- ``read`` places the drawn points by the depth model's own fit of the floor (the frame the
  obstacles are in) and keeps the route as far as it is on floor the camera can see.
- ``fit`` makes the path the chair's body can drive: the route as drawn if it clears
  everything, else slid a little to one side or the other until it does, and ending only
  where the body would touch something.
- How close to get to the goal, and when the chair has arrived, are the model's to judge:
  the harness treats the goal like anything else and only keeps the body from touching it.
- The runner (route_run.py) follows the path without stopping, asks for the next route
  while still driving, remembers what it has seen, and when it has to stop looks for a way
  on by itself (the same route fitted again, then from a little further back) before it
  asks the model for another.

Each look is asked afresh, with no earlier pictures: what the model needs to remember is
given to it in words (its own notes, what the chair did, where the goal is) and as a ring on
the picture where it last marked the goal.

Its instructions are in ``ml/route_pilot_prompt.md``, read afresh for every look. The key
comes from the environment (``GEMINI_API_KEY``; ``uv run --env-file .env`` sets it).

To see what it draws on recorded pictures, and what the harness makes of it, with nothing
connected to the chair::

    uv run --env-file .env python -m fsd_ml.route_pilot \\
        --say "go to the bottom of the stairs" runs/console/live/<run>/frames/0000123.jpg ...

That saves, under ``runs/routes/``, the picture as the model was shown it and the same
picture with its answer drawn: its points (green on floor, red not), the path the chair
would drive (solid), and the goal it marked.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from fsd_ml import candidates
from fsd_ml.chair import Body, ChairSetup
from fsd_ml.obstacles import Ground
from fsd_ml.pipeline.footprint import (
    BESIDE_M,
    SAMPLE_SPACING_M,
    first_hit_from_here,
    turn_room_deg,
)
from fsd_ml.pipeline.types import Obstacle

MODEL = os.environ.get("GEMINI_PILOT_MODEL", "gemini-robotics-er-2-preview")
# low, medium or high: more thinking is slower and may plan better.
THINKING = os.environ.get("GEMINI_PILOT_THINKING", "low")
PROMPT_FILE = Path(__file__).resolve().parent.parent / "route_pilot_prompt.md"

# What the model may answer with, and the turns among them (degrees to the left).
ACTIONS = ("route", "look_left", "look_right", "look_behind", "arrived", "give_up")
TURNS = {"look_left": 30.0, "look_right": -30.0, "look_behind": 180.0}
# A route point nearer than this is under the chair: the path starts at its middle anyway.
NEAR_M = 0.5
# No more of a route than this is taken at once: further off, a drawn point is too rough
# (a few pixels are tens of centimetres) and there will be a new look long before.
MAX_ROUTE_M = 3.5
# A route with less to drive than this is not worth setting off for.
MIN_ROUTE_M = 0.2
GOAL = (255, 60, 220, 255)

_POINT = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}
SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string"},
        "goal_visible": {"type": "boolean"},
        "goal": _POINT,
        "action": {"type": "string", "enum": list(ACTIONS)},
        "route": {"type": "array", "items": _POINT, "maxItems": 8},
        "note": {"type": "string"},
    },
    "required": ["scene", "goal_visible", "goal", "action", "route", "note"],
}


@dataclass(frozen=True, slots=True)
class Plan:
    """What the model answered. Points are fractions of the picture: (across, down)."""

    action: str
    points: tuple[tuple[float, float], ...] = ()
    goal: tuple[float, float] | None = None
    scene: str = ""
    note: str = ""


def _fraction(point: Any) -> tuple[float, float] | None:
    """A model's ``[y, x]`` (0-1000) as (across, down) fractions, or None if it is not one."""
    try:
        down, across = (float(value) / 1000.0 for value in point)
    except (TypeError, ValueError):
        return None
    return (across, down) if 0 <= across <= 1 and 0 <= down <= 1 else None


@dataclass(slots=True)
class RoutePilot:
    """One task's pilot. Every look is asked afresh: it keeps no conversation."""

    task: str
    model: str = MODEL
    thinking: str = THINKING
    client: Any = None
    prompt_file: Path = PROMPT_FILE
    # How the last reply went, for the record: what was sent, what came back, time, tokens.
    last: dict[str, Any] = field(default_factory=dict)

    def next(self, picture: bytes, report: str) -> Plan | None:
        """Show the model the picture and what has happened; get its plan.

        ``report`` is everything it is told in words. None if it gave no usable answer.
        """
        from google.genai import types

        from fsd_ml.gemini_pilot import client

        if self.client is None:
            self.client = client()
        text = f"Task: {self.task}\n\n{report}"
        started = time.perf_counter()
        response = self.client.models.generate_content(
            model=self.model,
            contents=[types.Part.from_bytes(data=picture, mime_type="image/jpeg"), text],
            config=types.GenerateContentConfig(
                system_instruction=self.prompt_file.read_text(),
                thinking_config=types.ThinkingConfig(thinking_level=self.thinking),
                response_mime_type="application/json",
                response_json_schema=SCHEMA,
            ),
        )
        ms = round((time.perf_counter() - started) * 1000.0)
        try:
            answer = json.loads(response.text or "")
        except ValueError:
            answer = {}
        usage = response.usage_metadata
        self.last = {
            "answer": answer,
            "prompt": text,
            "raw": response.text,
            "ms": ms,
            "model": self.model,
            "thinking": self.thinking,
            "input_tokens": usage.prompt_token_count if usage else None,
            "output_tokens": usage.candidates_token_count if usage else None,
            "thinking_tokens": usage.thoughts_token_count if usage else None,
        }
        if not isinstance(answer, dict) or answer.get("action") not in ACTIONS:
            return None
        points = [_fraction(point) for point in answer.get("route") or []]
        return Plan(
            action=answer["action"],
            points=tuple(point for point in points if point),
            goal=_fraction(answer.get("goal")) if answer.get("goal_visible") else None,
            scene=str(answer.get("scene") or ""),
            note=str(answer.get("note") or ""),
        )


@dataclass(frozen=True, slots=True)
class Route:
    """A drawn route as the harness read it, in the chair's frame when the picture was taken."""

    # Each drawn point: metres ahead, metres left, and whether it is on floor the camera sees.
    points: tuple[tuple[float, float, bool], ...]
    # The path to drive: a point every 0.1 m from the chair's middle. Empty if none can be.
    path: np.ndarray
    # The drawn points the path goes through (those on floor the camera sees).
    kept: tuple[tuple[float, float], ...] = ()


def place(
    point: tuple[float, float], ground: Ground | None, setup: ChairSetup
) -> tuple[float, float, bool]:
    """Where a point of the picture is: metres ahead and left of the chair's middle, and
    whether it is on floor. By the depth model's floor when there is one (``ground``), else
    as if everything below the horizon were floor."""
    if ground is not None:
        return ground.at(*point)
    ((ahead, left),) = setup.to_floor([[point[0] * 1000, point[1] * 1000]], 1000, 1000)
    return float(ahead), float(left), bool(np.isfinite(ahead))


def smooth(points: Sequence[tuple[float, float]]) -> np.ndarray:
    """A line from the chair's middle through the points, with a point every 0.1 m and its
    corners rounded, so the chair can follow it without stopping to turn."""
    line = np.array([(0.0, 0.0), *points], dtype=np.float64)
    along = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(line, axis=0), axis=1))])
    if along[-1] < SAMPLE_SPACING_M:
        return np.empty((0, 2))
    at = np.arange(0.0, along[-1] + 1e-9, SAMPLE_SPACING_M)
    path = np.column_stack([np.interp(at, along, line[:, 0]), np.interp(at, along, line[:, 1])])
    for _ in range(2):  # each pass pulls a point toward the middle of its neighbours
        if len(path) > 4:
            inner = np.column_stack(
                [np.convolve(path[:, axis], np.ones(5) / 5, mode="valid") for axis in (0, 1)]
            )
            path = np.vstack([path[:2], inner, path[-2:]])
    return path[1:]


# How far a route may be moved to one side to get the chair's body past things, in the order
# tried: as drawn first, then a little either way, then more.
SLIDES_M = (0.0, 0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.4, -0.4, 0.5, -0.5, 0.6, -0.6)
# A slide comes on over this much of the route from the chair, which is where it is.
SLIDE_OVER_M = 0.6


def read(
    plan: Plan, ground: Ground | None, setup: ChairSetup
) -> tuple[tuple[tuple[float, float, bool], ...], list[tuple[float, float]]]:
    """Where the drawn points are (chair's frame, with whether each is on floor), and the
    ones to drive through: up to the first that is not on floor the camera can see, or is
    too far off to place."""
    points = tuple(place(point, ground, setup) for point in plan.points)
    kept: list[tuple[float, float]] = []
    for ahead, left, floor in points:
        if not floor or math.hypot(ahead, left) > MAX_ROUTE_M:
            break
        if math.hypot(ahead, left) >= NEAR_M and ahead > 0:
            kept.append((ahead, left))
    return points, kept


def slid(path: np.ndarray, by_m: float) -> np.ndarray:
    """A path moved ``by_m`` to its left (negative: right), coming on from nothing at the
    chair, so it still starts where the chair is."""
    if not len(path) or by_m == 0:
        return path
    step = np.gradient(np.vstack([[0.0, 0.0], path]), axis=0)[1:]
    across = np.column_stack([-step[:, 1], step[:, 0]])
    across /= np.maximum(np.linalg.norm(across, axis=1, keepdims=True), 1e-9)
    along = np.arange(1, len(path) + 1) * SAMPLE_SPACING_M
    moved = path + across * (by_m * np.minimum(1.0, along / SLIDE_OVER_M))[:, None]
    # Spaced evenly again, a point every 0.1 m: a distance along it is then a count of points.
    line = np.vstack([[0.0, 0.0], moved])
    gone = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(line, axis=0), axis=1))])
    at = np.arange(SAMPLE_SPACING_M, gone[-1] + 1e-9, SAMPLE_SPACING_M)
    return np.column_stack([np.interp(at, gone, line[:, 0]), np.interp(at, gone, line[:, 1])])


def fit(
    kept: Sequence[tuple[float, float]],
    obstacles: Sequence[Obstacle],
    setup: ChairSetup,
    margin_m: float,
) -> np.ndarray:
    """The path the chair drives for a route through ``kept`` (chair's frame).

    Where the model wants to go is the model's business; getting the chair's body past
    things on the way there is the harness's. So the route is tried as drawn, and if the
    body would not clear something, slid a little to one side and then the other (SLIDES_M)
    until it does, taking the first that is clear to its end, else the one that gets
    furthest. A path ends only where the chair's body, with ``margin_m`` round it, would
    touch something: the goal is treated like anything else, so the chair can be drawn right
    up to it.
    """
    base = smooth(list(kept))
    shape = Body(setup.chair.width_m, setup.chair.length_m, margin_m)
    best = np.empty((0, 2))
    for by_m in SLIDES_M:
        path = slid(base, by_m)
        hit = first_hit_from_here(path, obstacles, shape)
        # The hit is counted in points from the chair, and the path's first point is one
        # step out: so this keeps up to the last point that touches nothing.
        clear = len(path) if hit is None else max(0, round(hit[0] / SAMPLE_SPACING_M) - 1)
        if clear > len(best):
            best = path[:clear]
        if clear == len(path):
            break
    return best if len(best) * SAMPLE_SPACING_M >= MIN_ROUTE_M else np.empty((0, 2))


def interpret(
    plan: Plan,
    ground: Ground | None,
    obstacles: Sequence[Obstacle],
    setup: ChairSetup,
    margin_m: float,
) -> Route:
    """A drawn route as the path the chair will drive: ``read``, then ``fit``."""
    points, kept = read(plan, ground, setup)
    return Route(points, fit(kept, obstacles, setup, margin_m), kept=tuple(kept))


# The chair's place in a run: metres ahead and left of where the run began, and its heading
# (radians, positive left). Believed, not measured: the gyro's heading and speed times time.
Pose = tuple[float, float, float]


def out_of(pose: Pose, points_xy: np.ndarray) -> np.ndarray:
    """Points in the chair's frame at ``pose``, as points in the run's frame."""
    x, y, heading = pose
    ahead, left = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2).T
    return np.column_stack(
        [
            x + ahead * math.cos(heading) - left * math.sin(heading),
            y + ahead * math.sin(heading) + left * math.cos(heading),
        ]
    )


def into(pose: Pose, points_xy: np.ndarray) -> np.ndarray:
    """Points in the run's frame, as points in the chair's frame at ``pose``."""
    x, y, heading = pose
    east, north = (np.asarray(points_xy, dtype=np.float64).reshape(-1, 2) - [x, y]).T
    return np.column_stack(
        [
            east * math.cos(heading) + north * math.sin(heading),
            -east * math.sin(heading) + north * math.cos(heading),
        ]
    )


# How big the cells are that obstacles and seen floor are remembered in.
REMEMBER_CELL_M = 0.15
# A remembered obstacle is taken to be bigger by this share of how far the chair has driven
# since it was last seen: where it is, is only as good as the chair's sense of how far it
# has gone, which is speed times time.
DRIFT_SHARE = 0.08
# Floor counts as seen from this near the lens to this far, and this much inside the edge
# of the picture.
SEEN_NEAR_M, SEEN_FAR_M, SEEN_EDGE_DEG = 0.45, 3.0, 6.0
# Where a run starts, the floor this far round the point the chair turns about is taken to
# be clear: the person who started it put the chair where it can turn. Nothing else the
# camera has not looked at is.
START_CLEAR_M = 1.1
# A remembered obstacle up to this far behind one seen now is an older sighting of the same
# thing, placed a little off by the chair's drifting sense of where it is: it is replaced.
SAME_THING_M = 0.5


class Remembered:
    """What the camera has seen in this run, kept after it leaves the picture: the
    obstacles, and the floor that was seen to be clear.

    The obstacle sense only knows what is in the current picture, and the chair's body is
    beside and behind the camera: a thing the chair is passing leaves the picture while the
    chair can still hit it, and a turn on the spot swings the body over floor the camera is
    not looking at. So both are kept, in the run's frame, for the whole run. An obstacle is
    forgotten only when a later picture looks straight at where it was and it is not there.
    Floor is known clear only if the camera saw it with nothing on it or the chair has stood
    on it; floor that was never seen is not clear, it is unknown.
    """

    def __init__(self, setup: ChairSetup, pose: Pose) -> None:
        self._setup = setup
        # By cell: where (run's frame), how big, when last seen, and how far the chair had
        # driven by then.
        self._cells: dict[tuple[int, int], tuple[float, float, float, float, float]] = {}
        self._driven_m = 0.0
        # The cells of floor known to be clear.
        self._clear: set[tuple[int, int]] = set()
        camera = setup.camera
        # The floor a picture shows, as points in the chair's frame, with each one's bearing
        # and distance from the lens.
        half = camera.hfov_deg / 2 - SEEN_EDGE_DEG
        bearing, distance = np.meshgrid(
            np.radians(np.arange(-half, half + 1e-9, 2.0)), np.arange(SEEN_NEAR_M, SEEN_FAR_M, 0.12)
        )
        self._bearing, self._distance = bearing.ravel(), distance.ravel()
        self._view = (
            np.column_stack(
                [self._distance * np.cos(self._bearing), self._distance * np.sin(self._bearing)]
            )
            + setup.lens_xy
        )
        # The chair's own body, as points in its frame.
        body = setup.chair
        along = np.arange(-body.length_m / 2, body.length_m / 2 + 0.05, 0.1)
        across = np.arange(-body.width_m / 2, body.width_m / 2 + 0.05, 0.1)
        self._body = np.array([(a, b) for a in along for b in across])
        ring = [
            (r * math.cos(a), r * math.sin(a))
            for r in np.arange(0.0, START_CLEAR_M + 1e-9, 0.1)
            for a in np.arange(0.0, 2 * math.pi, 0.1)
        ]
        self._mark(out_of(pose, np.array(ring) + [-body.pivot_behind_m, 0.0]))

    @staticmethod
    def _keys(points_xy: np.ndarray) -> set[tuple[int, int]]:
        return {(int(a), int(b)) for a, b in np.round(points_xy / REMEMBER_CELL_M)}

    def _mark(self, points_xy: np.ndarray) -> None:
        self._clear |= self._keys(points_xy)

    def stand(self, pose: Pose) -> None:
        """The chair is here: the floor under it is clear."""
        self._mark(out_of(pose, self._body))

    def _in_sight(
        self, points_xy: np.ndarray, obstacles: Sequence[Obstacle], behind_m: float = 0.0
    ) -> np.ndarray:
        """Which points (chair's frame) a picture taken from here shows the floor at: in
        view, in range, and not behind or under any of the obstacles it found. A point less
        than ``behind_m`` behind an obstacle still counts as shown: that is how a remembered
        copy of the same thing, placed a little off, is told from something hidden by it."""
        ahead, left = (
            np.asarray(points_xy, dtype=np.float64).reshape(-1, 2) - self._setup.lens_xy
        ).T
        bearing, distance = np.arctan2(left, ahead), np.hypot(ahead, left)
        half = math.radians(self._setup.camera.hfov_deg / 2 - SEEN_EDGE_DEG)
        shown = (np.abs(bearing) < half) & (distance > SEEN_NEAR_M) & (distance < SEEN_FAR_M)
        for obstacle in obstacles:
            o_ahead, o_left = (
                obstacle.x - self._setup.lens_xy[0],
                obstacle.y - self._setup.lens_xy[1],
            )
            o_distance = math.hypot(o_ahead, o_left)
            wide = math.atan2(obstacle.radius_m + 0.08, max(o_distance, 0.1))
            behind = np.abs(bearing - math.atan2(o_left, o_ahead)) < wide
            shown &= ~(behind & (distance > o_distance - obstacle.radius_m - 0.08 + behind_m))
        return shown

    def see(self, obstacles: Sequence[Obstacle], pose: Pose, at_s: float, driven_m: float) -> None:
        """Take in what one picture showed, taken at ``pose`` at time ``at_s``, when the
        chair had driven ``driven_m`` in all."""
        self._driven_m = driven_m
        if self._cells:
            # Forget what this picture looked straight at and did not find again.
            placed = into(pose, np.array([cell[:2] for cell in self._cells.values()]))
            gone = self._in_sight(placed, obstacles, SAME_THING_M)
            self._cells = {
                key: cell
                for (key, cell), lost in zip(self._cells.items(), gone, strict=True)
                if not lost
            }
        self._mark(out_of(pose, self._view[self._in_sight(self._view, obstacles)]))
        if obstacles:
            where = out_of(pose, np.array([[o.x, o.y] for o in obstacles]))
            for (x, y), obstacle in zip(where, obstacles, strict=True):
                key = (round(x / REMEMBER_CELL_M), round(y / REMEMBER_CELL_M))
                self._cells[key] = (float(x), float(y), obstacle.radius_m, at_s, driven_m)
            self._clear -= set(self._cells)

    def around(self, pose: Pose, driven_m: float | None = None) -> list[Obstacle]:
        """Everything remembered, in the chair's frame at ``pose``, each as much bigger as
        the chair may have lost track of it since (DRIFT_SHARE)."""
        driven_m = self._driven_m if driven_m is None else driven_m
        if not self._cells:
            return []
        cells = list(self._cells.values())
        where = into(pose, np.array([cell[:2] for cell in cells]))
        return [
            Obstacle(
                f"m{n}",
                "object",
                float(ahead),
                float(left),
                radius_m=cell[2] + DRIFT_SHARE * max(0.0, driven_m - cell[4]),
            )
            for n, ((ahead, left), cell) in enumerate(zip(where, cells, strict=True))
        ]

    def turn_room(self, pose: Pose, degrees_left: float, driven_m: float) -> tuple[float, str]:
        """How far of a turn on the spot the chair may make from ``pose``, and what stops
        it going further ("" if nothing).

        Its body must not touch anything remembered (footprint.turn_room_deg), and must
        swing only over floor known to be clear: where the camera never looked, there may
        be anything. In 5 degree steps.
        """
        body = self._setup.chair
        room = turn_room_deg(degrees_left, self.around(pose, driven_m), body)
        why = "" if room == degrees_left else "its body would hit something it saw earlier"
        pivot = np.array([-body.pivot_behind_m, 0.0])
        way, known = math.copysign(1.0, degrees_left), 0.0
        for step in np.arange(5.0, abs(room) + 5.0, 5.0):
            angle = math.radians(way * min(step, abs(room)))
            turn = np.array(
                [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]]
            )
            swung = (self._body - pivot) @ turn.T + pivot
            if not self._keys(out_of(pose, swung)) <= self._clear | set(self._cells):
                return way * known, "its body would swing over floor the camera has not seen"
            known = min(step, abs(room))
        return room, why

    def room_to_back(self, pose: Pose, metres: float, driven_m: float) -> bool:
        """Whether the chair may back straight up ``metres`` from ``pose``: over floor known
        to be clear (it has usually just come from there) and into nothing remembered."""
        back = np.column_stack([-np.arange(0.1, metres + 1e-9, 0.1), np.zeros(round(metres / 0.1))])
        body = self._setup.chair
        tight = Body(body.width_m, body.length_m, BESIDE_M)
        if first_hit_from_here(back, self.around(pose, driven_m), tight) is not None:
            return False
        return self._keys(out_of(pose, self._body + back[-1])) <= self._clear


def hidden(goal: tuple[float, float], ground: Ground | None, setup: ChairSetup) -> bool:
    """Whether a remembered place (chair's frame) is behind something in this picture: what
    the picture shows there is over a metre nearer than the place itself. A ring drawn
    there would sit on the thing in front, and point the model at the wrong thing."""
    if ground is None:
        return False
    ((x, y),) = setup.to_pixels([goal], 1000, 1000)
    if not (0 <= x < 1000 and 0 <= y < 1000):
        return False
    ahead, left, _ = ground.at(x / 1000, y / 1000)
    return math.hypot(ahead, left) < math.hypot(*goal) - 1.0


def whereabouts(ahead: float, left: float) -> str:
    """A place relative to the chair in words: "2.3 m away, 15° to the right"."""
    bearing = math.degrees(math.atan2(left, ahead))
    side = (
        "straight ahead"
        if abs(bearing) < 5
        else f"{abs(bearing):.0f}° to the {'left' if bearing > 0 else 'right'}"
    )
    if abs(bearing) > 120:
        side = "behind the chair"
    return f"{math.hypot(ahead, left):.1f} m away, {side}"


def report(
    look: int,
    moving: str,
    happened: Sequence[str],
    pose: Pose,
    goal: tuple[float, float] | None,
    notes: Sequence[str],
    half_view_deg: float,
    goal_hidden: bool = False,
) -> str:
    """What the model is told in words with a picture, kept short and to what it decides
    by: what the chair did, where the goal it marked is believed to be, and its own notes.
    Nothing about obstacles or clearance: getting past things is the harness's work."""
    x, y, heading = pose
    turned = math.degrees(heading)
    lines = [f"Look {look}. {moving}"]
    lines += [f"- {line}" for line in happened]
    if goal is None:
        lines.append("You have not marked the goal yet.")
    else:
        bearing = abs(math.degrees(math.atan2(goal[1], goal[0])))
        seen = "the ring in the picture"
        if goal_hidden:
            seen = "behind something, so not drawn"
        elif bearing >= half_view_deg or goal[0] <= 0:
            seen = "out of the picture"
        lines.append(f"The goal as you last marked it: {whereabouts(*goal)} ({seen}).")
    lines.append(
        f"Since the start: turned {abs(turned):.0f}° to the {'left' if turned >= 0 else 'right'},"
        f" {math.hypot(x, y):.1f} m from where it began."
    )
    if notes:
        lines += ["Your last notes:"] + [f"- {note}" for note in notes[-2:]]
    return "\n".join(lines)


def picture(jpeg: bytes, setup: ChairSetup, goal: tuple[float, float] | None = None) -> bytes:
    """The camera's JPEG as the model is shown it: the picture, the guide lines, and a ring
    where it last marked the goal (``goal``, in the chair's frame now), if that is in view.
    Nothing else: no obstacles are drawn on it."""
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if image.width < candidates.DRAW_WIDTH:
        scale = candidates.DRAW_WIDTH / image.width
        image = image.resize(
            (candidates.DRAW_WIDTH, round(image.height * scale)), Image.Resampling.LANCZOS
        )
    width, height = image.size
    pen = ImageDraw.Draw(image, "RGBA")
    candidates.guides(pen, setup, image.size)
    if goal is not None:
        spot = candidates.in_view(setup.to_pixels([goal], width, height), width, height)
        if len(spot) and spot[0][1] < height:
            gx, gy = spot[0]
            pen.ellipse([gx - 16, gy - 16, gx + 16, gy + 16], outline=GOAL, width=4)
            pen.text(
                (gx, gy - 28),
                "GOAL",
                fill=GOAL,
                font=ImageFont.load_default(size=18),
                anchor="mm",
                stroke_width=2,
                stroke_fill=(0, 0, 0, 255),
            )
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()


def answered(shown: bytes, plan: Plan, route: Route, setup: ChairSetup) -> bytes:
    """The picture the model was shown with its answer drawn on, for a person to check: its
    points (green on floor, red not), the path the chair would drive, the goal it marked."""
    image = Image.open(io.BytesIO(shown)).convert("RGB")
    width, height = image.size
    pen = ImageDraw.Draw(image, "RGBA")
    drawn = [(across * width, down * height) for across, down in plan.points]
    if len(drawn) > 1:
        pen.line(drawn, fill=(255, 255, 255, 160), width=2)
    if len(route.path) > 1:
        line = candidates.in_view(setup.to_pixels(route.path, width, height), width, height)
        if len(line) > 1:
            pen.line([tuple(p) for p in line], fill=(40, 255, 90, 255), width=6, joint="curve")
    font = ImageFont.load_default(size=16)
    for n, ((x, y), (_, _, floor)) in enumerate(zip(drawn, route.points, strict=False), start=1):
        colour = (40, 220, 80, 255) if floor else (255, 50, 50, 255)
        pen.ellipse([x - 11, y - 11, x + 11, y + 11], fill=colour, outline=(0, 0, 0, 255))
        pen.text((x, y), str(n), fill="black", font=font, anchor="mm")
    if plan.goal:
        gx, gy = plan.goal[0] * width, plan.goal[1] * height
        pen.rectangle([gx - 12, gy - 12, gx + 12, gy + 12], outline=GOAL, width=4)
    pen.rectangle([0, 0, width, 24], fill=(0, 0, 0, 200))
    words = f"{plan.action} · {len(route.path) * SAMPLE_SPACING_M:.1f} m to drive · {plan.note}"
    pen.text((6, 4), words[:150], fill="white", font=font)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()


def main() -> None:
    from fsd_ml import bench, chair, obstacles

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pictures", nargs="+", type=Path, help="recorded camera JPEGs")
    parser.add_argument("--say", default="go to the bottom of the stairs", help="the task")
    parser.add_argument("--out", type=Path, default=Path("runs/routes"))
    parser.add_argument(
        "--times",
        type=int,
        default=1,
        help="ask again up to this often while no route can be driven",
    )
    args = parser.parse_args()
    setup, settings = chair.load(), bench.DEFAULTS
    camera = setup.camera
    depth = obstacles.DepthObstacles() if obstacles.available() else None
    margin = settings["path_clearance_m"]
    args.out.mkdir(parents=True, exist_ok=True)
    pilot = RoutePilot(args.say)
    for source in args.pictures:
        jpeg = source.read_bytes()
        ground, seen = None, []
        if depth is not None:
            rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))
            cells = depth.depth(rgb)
            lens = (camera.hfov_deg, camera.height_m, setup.lens_xy, camera.tilt_down_deg)
            ground = obstacles.ground_from_depth(cells, *lens)
            seen, _ = obstacles.obstacles_from_depth(cells, *lens)
        shown = picture(jpeg, setup)
        name = f"{source.parent.parent.name}-{source.stem}"
        (args.out / f"{name}.shown.jpg").write_bytes(shown)
        for n in range(args.times):
            said = report(
                n + 1, "The chair is standing still.", [], (0.0, 0.0, 0.0), None, [], 50.0
            )
            plan = pilot.next(shown, said)
            if plan is None:
                print(f"{source} #{n}: no usable answer: {pilot.last.get('raw')}")
                continue
            route = interpret(plan, ground, seen, setup, margin)
            saved = args.out / f"{name}.{n}.jpg"
            saved.write_bytes(answered(shown, plan, route, setup))
            print(
                f"{source} #{n}: {plan.action}, {pilot.last['ms']} ms,"
                f" {len(plan.points)} points, {sum(p[2] for p in route.points)} on floor,"
                f" {len(route.path) * SAMPLE_SPACING_M:.1f} m to drive\n"
                f"  goal: {plan.goal}  note: {plan.note}\n  {saved}"
            )


if __name__ == "__main__":
    main()
