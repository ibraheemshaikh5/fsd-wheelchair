"""Gemini drives the chair by drawing its route on the camera's picture.

A model that sees is poor at metres and degrees, and at picking among ready-made moves one
small step at a time; it is good at pointing. So each look it is shown the picture (with
parking-camera guide lines) and draws the way the chair should go as a few points on the
floor, or chooses to look round, back up, or stop. It decides where to go, which way round
things, when to search and when it has arrived.

The harness makes that happen and says honestly how it went:

- ``interpret`` turns the drawn points into a path in metres. Each point is placed by the
  depth model's own fit of the floor (the frame the obstacles are in), so a route and what
  it must clear are measured the same way. A route is kept only as far as it is on floor
  the camera can see: the first point that lies on a thing, or beyond what depth can be
  trusted for, ends it. Then it is cut where the chair would stop for an obstacle anyway
  (the one stop rule, pipeline/footprint.stop_distance_m). Nothing is moved or re-routed:
  what was cut, and why, is told to the model with its next picture.
- The runner (moves.Runner.route) follows the path without stopping, asks for the next
  route while still driving, and carries the new one over to where the chair has got to.

Each look is asked afresh, with no earlier pictures: what the model needs to remember is
given to it in words (its own notes, what the chair did, how far it has turned and moved)
and drawn on the picture (what is left of its last route, and where it last marked the goal).

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
from fsd_ml.pipeline.footprint import SAMPLE_SPACING_M, first_hit
from fsd_ml.pipeline.types import Obstacle

MODEL = os.environ.get("GEMINI_PILOT_MODEL", "gemini-robotics-er-2-preview")
# low, medium or high: more thinking is slower and may plan better.
THINKING = os.environ.get("GEMINI_PILOT_THINKING", "low")
PROMPT_FILE = Path(__file__).resolve().parent.parent / "route_pilot_prompt.md"

# What the model may answer with, and the turns among them (degrees to the left).
ACTIONS = ("route", "look_left", "look_right", "look_behind", "back_up", "arrived", "give_up")
TURNS = {"look_left": 60.0, "look_right": -60.0, "look_behind": 180.0}
BACK_UP_M = 0.3
# A route point nearer than this is under the chair: the path starts at its middle anyway.
NEAR_M = 0.5
# No more of a route than this is taken at once: further off, a drawn point is too rough
# (a few pixels are tens of centimetres) and there will be a new look long before.
MAX_ROUTE_M = 3.5
# A route with less to drive than this is not worth setting off for.
MIN_ROUTE_M = 0.3
PREVIOUS = (60, 220, 255, 230)
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
    # What to tell the model about how its route was read; "" when all of it is driven.
    said: str = ""


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


def interpret(
    plan: Plan,
    ground: Ground | None,
    obstacles: Sequence[Obstacle],
    setup: ChairSetup,
    margin_m: float,
    stop_m: float,
) -> Route:
    """Turn a drawn route into the path the chair can drive, and say what was left out.

    Kept: the points up to the first that is not on floor the camera can see. Cut: where the
    chair's body would come within ``margin_m`` of an obstacle, less ``stop_m`` (so it ends
    where the chair would stop anyway). Never moved sideways or re-routed.
    """
    points = tuple(place(point, ground, setup) for point in plan.points)
    kept: list[tuple[float, float]] = []
    left_out = ""
    for n, (ahead, left, floor) in enumerate(points):
        if not floor or math.hypot(ahead, left) > MAX_ROUTE_M:
            what = "is not on floor the chair can see" if not floor else "is too far to place"
            rest = f"point {n + 1}" if n + 1 == len(points) else f"points {n + 1} to {len(points)}"
            left_out = (
                f"{rest} of your route were left out: point {n + 1} {what}"
                f" ({'it is on a thing, or hidden' if not floor else 'over 3.5 m away'})."
            )
            break
        if math.hypot(ahead, left) >= NEAR_M and ahead > 0:
            kept.append((ahead, left))
    path = smooth(kept)
    hit = first_hit(path, obstacles, Body(setup.chair.width_m, setup.chair.length_m, margin_m))
    blocked = ""
    if hit is not None:
        clear = max(0, round((hit[0] - stop_m) / SAMPLE_SPACING_M) - 1)
        if clear < len(path):
            side = "left" if hit[1].y > 0 else "right"
            gap = max(0.0, hit[1].x - setup.chair.length_m / 2)
            blocked = (
                f"the chair can follow it for {clear * SAMPLE_SPACING_M:.1f} m only: past that"
                f" its body would come within {margin_m:.1f} m of something that is {gap:.1f} m"
                f" ahead of the chair's front and {abs(hit[1].y):.1f} m to the {side}"
                " (as seen when the picture was taken)."
            )
            path = path[:clear]
    if len(path) * SAMPLE_SPACING_M < MIN_ROUTE_M:
        why = " ".join(part for part in (left_out, blocked) if part) or "it has no floor points."
        return Route(points, np.empty((0, 2)), f"None of your route could be driven: {why}")
    said = " ".join(part[0].upper() + part[1:] for part in (left_out, blocked) if part)
    return Route(points, path, said)


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


# How long an obstacle that has left the picture is still kept clear of, and how big the
# cells are that they are remembered in.
REMEMBER_S = 20.0
REMEMBER_CELL_M = 0.15


class Remembered:
    """Obstacles the camera has seen in this run, kept after they leave the picture.

    The obstacle sense only knows what is in the current picture, and the chair's body is
    beside and behind the camera: a thing the chair is passing leaves the picture while the
    chair can still hit it. So what was seen is kept, in the run's frame, for REMEMBER_S.
    What is in the picture now always replaces what was remembered there, so nothing that
    has moved away, and no doubled copy from a drifting sense of position, stays in view.
    """

    def __init__(self, setup: ChairSetup) -> None:
        self._setup = setup
        self._cells: dict[tuple[int, int], tuple[float, float, float, float]] = {}

    def see(self, obstacles: Sequence[Obstacle], pose: Pose, at_s: float) -> None:
        """Take in what one picture showed, taken at ``pose`` at time ``at_s``."""
        camera = self._setup.camera
        if self._cells:
            # Forget what this picture should have shown again: it is in view and in range.
            placed = np.array([cell[:2] for cell in self._cells.values()])
            ahead, left = (into(pose, placed) - self._setup.lens_xy).T
            aside = np.abs(np.degrees(np.arctan2(left, ahead)))
            shown = (
                (aside < camera.hfov_deg / 2 - 8) & (ahead > 0.6) & (np.hypot(ahead, left) < 3.0)
            )
            old = np.array([at_s - cell[3] > REMEMBER_S for cell in self._cells.values()])
            self._cells = {
                key: cell
                for (key, cell), gone in zip(self._cells.items(), shown | old, strict=True)
                if not gone
            }
        if obstacles:
            where = out_of(pose, np.array([[o.x, o.y] for o in obstacles]))
            for (x, y), obstacle in zip(where, obstacles, strict=True):
                key = (round(x / REMEMBER_CELL_M), round(y / REMEMBER_CELL_M))
                self._cells[key] = (float(x), float(y), obstacle.radius_m, at_s)

    def around(self, pose: Pose) -> list[Obstacle]:
        """Everything remembered, in the chair's frame at ``pose``."""
        if not self._cells:
            return []
        cells = list(self._cells.values())
        where = into(pose, np.array([cell[:2] for cell in cells]))
        return [
            Obstacle(f"m{n}", "object", float(ahead), float(left), radius_m=cell[2])
            for n, ((ahead, left), cell) in enumerate(zip(where, cells, strict=True))
        ]


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
) -> str:
    """Everything the model is told in words with a picture: what the chair is doing, what
    happened since its last answer, where the chair and the goal it marked are believed to
    be, and its own last notes."""
    x, y, heading = pose
    turned = math.degrees(heading)
    lines = [f"Look {look}. {moving}", "", "What happened since your last answer:"]
    lines += [f"- {line}" for line in happened] or ["- nothing yet."]
    lines += [
        "",
        f"Since the start the chair has turned {abs(turned):.0f}° to the"
        f" {'left' if turned >= 0 else 'right'} in all, and is {math.hypot(x, y):.1f} m from"
        " where it started (believed, not measured).",
    ]
    if goal is None:
        lines.append("You have not marked the goal yet.")
    else:
        bearing = abs(math.degrees(math.atan2(goal[1], goal[0])))
        seen = (
            "it is the magenta ring" if bearing < half_view_deg and goal[0] > 0 else "out of view"
        )
        lines.append(
            f"The goal as you last marked it is believed to be {whereabouts(*goal)} ({seen})."
        )
    lines += ["", "Your last notes, oldest first:"]
    lines += [f"- {note}" for note in notes[-4:]] or ["- none yet."]
    return "\n".join(lines)


def picture(
    jpeg: bytes,
    setup: ChairSetup,
    previous: np.ndarray | None = None,
    goal: tuple[float, float] | None = None,
) -> bytes:
    """The camera's JPEG as the model is shown it: the guide lines, a triangle at the
    chair's middle, what is left of its last route (``previous``, in the chair's frame now),
    and where it last marked the goal (``goal``, likewise), if that is in view."""
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if image.width < candidates.DRAW_WIDTH:
        scale = candidates.DRAW_WIDTH / image.width
        image = image.resize(
            (candidates.DRAW_WIDTH, round(image.height * scale)), Image.Resampling.LANCZOS
        )
    width, height = image.size
    pen = ImageDraw.Draw(image, "RGBA")
    candidates.guides(pen, setup, image.size)
    # The chair's middle: where the floor straight ahead of it meets the picture's bottom.
    ahead = np.arange(0.3, 1.5, 0.02)
    middle = setup.to_pixels(np.column_stack([ahead, np.zeros_like(ahead)]), width, height)
    inside = middle[~np.isnan(middle[:, 1]) & (middle[:, 1] <= height)]
    x = float(inside[0][0]) if len(inside) else width / 2
    pen.polygon(
        [(x - 16, height - 2), (x + 16, height - 2), (x, height - 30)],
        fill=(255, 255, 255, 235),
        outline=(0, 0, 0, 255),
    )
    if previous is not None and len(previous) > 1:
        line = candidates.in_view(setup.to_pixels(previous, width, height), width, height)
        for a, b in zip(line[:-1:2], line[1::2], strict=False):
            pen.line([tuple(a), tuple(b)], fill=PREVIOUS, width=4)
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
    from fsd_ml.pipeline.footprint import stop_distance_m

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pictures", nargs="+", type=Path, help="recorded camera JPEGs")
    parser.add_argument("--say", default="go to the bottom of the stairs", help="the task")
    parser.add_argument("--out", type=Path, default=Path("runs/routes"))
    parser.add_argument("--times", type=int, default=1, help="ask this many times per picture")
    args = parser.parse_args()
    setup, settings = chair.load(), bench.DEFAULTS
    camera = setup.camera
    depth = obstacles.DepthObstacles() if obstacles.available() else None
    margin = settings["path_clearance_m"]
    stop_m = stop_distance_m(settings["drive_stick_pct"] / 100 * settings["v_full_mps"])
    args.out.mkdir(parents=True, exist_ok=True)
    pilot = RoutePilot(args.say)
    for source in args.pictures:
        jpeg = source.read_bytes()
        ground, seen = None, []
        if depth is not None:
            rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))
            cells = depth.depth(rgb)
            lens = (camera.hfov_deg, camera.height_m, setup.lens_xy)
            ground = obstacles.ground_from_depth(cells, *lens)
            seen, _ = obstacles.obstacles_from_depth(cells, *lens)
        shown = picture(jpeg, setup)
        name = f"{source.parent.parent.name}-{source.stem}"
        (args.out / f"{name}.shown.jpg").write_bytes(shown)
        for n in range(args.times):
            plan = pilot.next(shown, "This is the first look. The chair is standing still.")
            if plan is None:
                print(f"{source} #{n}: no usable answer: {pilot.last.get('raw')}")
                continue
            route = interpret(plan, ground, seen, setup, margin, stop_m)
            saved = args.out / f"{name}.{n}.jpg"
            saved.write_bytes(answered(shown, plan, route, setup))
            print(
                f"{source} #{n}: {plan.action}, {pilot.last['ms']} ms,"
                f" {len(plan.points)} points, {sum(p[2] for p in route.points)} on floor,"
                f" {len(route.path) * SAMPLE_SPACING_M:.1f} m to drive\n"
                f"  goal: {plan.goal}  note: {plan.note}\n  told: {route.said or 'all driven'}"
                f"\n  {saved}"
            )


if __name__ == "__main__":
    main()
