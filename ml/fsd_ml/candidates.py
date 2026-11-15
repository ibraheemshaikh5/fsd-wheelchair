"""The ways the chair could go next, drawn on the camera's picture for a model to pick from.

A model that sees is poor at judging metres and degrees, and good at pointing. So it is not
asked for a distance or an angle: a fan of paths is drawn on the floor of the picture, each
with a number at its far end, and it answers with a number.

    paths = fan(obstacles, setup, 0.2, stop_m)  # {"1": path, ...}, blocked ones left out
    picture = draw(jpeg, paths, setup)   # the JPEG with the paths and their numbers on it
    listed = options(paths)                   # {"1": "path 1: curving left ...", ..., "L": ...}

Each path is an arc from the middle of the chair, leaving straight ahead and ending AIM_M
away in one of BEARINGS_DEG. What is offered is the part of it the chair fits along and the
camera can see the floor of (nothing is known about what is out of view), in the chair's
frame (metres ahead, metres left), with a point every 0.1 m. Number 1 is always the leftmost
and 4 is always straight ahead, whichever are on offer.

Every picture also gets guide lines, as a parking camera has: where the chair's two sides
would pass if it drove straight, and marks 0.5, 1 and 2 m in front of it.
"""

from __future__ import annotations

import io
import math
from collections.abc import Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from fsd_ml.chair import Body, ChairSetup
from fsd_ml.pipeline.footprint import SAMPLE_SPACING_M, first_hit
from fsd_ml.pipeline.types import Obstacle

# Where each path ends, as seen from the middle of the chair: degrees to the left of straight
# ahead, in the order they are numbered. The camera sees 40 degrees each side, so the
# outermost leave the picture before they end.
BEARINGS_DEG = (60, 40, 20, 0, -20, -40, -60)
AIM_M = 2.5
# A path is not offered past where it comes this close to the side of the camera's view.
VIEW_EDGE_DEG = 3.0
# The nearest floor in the picture is about this far ahead of the lens: nearer than that a
# path is under the picture, not beside it.
NEAR_M = 0.4
# A path ends this much further back than the stop rule asks, so that the small errors in
# how far the chair believes it has gone do not trip the stop just before the end.
PLAN_SLACK_M = 0.1
# A path with less clear floor than this is not offered.
MIN_M = 0.3
# What can be picked besides a path: turns on the spot (degrees to the left), and the ends.
TURNS = {"L": 60.0, "R": -60.0, "B": 180.0}
OTHERS = {
    "L": "turn left 60° on the spot",
    "R": "turn right 60° on the spot",
    "B": "turn around (180°) on the spot",
    "S": "stop: the task is done, the chair has arrived",
    "X": "give up: the task cannot be done",
}
COLOURS = ("#ff5a5a", "#ff9f1c", "#ffe14d", "#5dff7a", "#3ddbd9", "#5a9bff", "#d17bff")
# Pictures narrower than this are enlarged before drawing, so the numbers stay readable.
DRAW_WIDTH = 960
# The guide lines' marks, in metres from the chair's front.
MARKS_M = (0.5, 1.0, 2.0)
GUIDE = (255, 255, 255, 150)


def arc(bearing_deg: float) -> np.ndarray:
    """The whole path that leaves straight ahead and ends AIM_M away, ``bearing_deg`` left."""
    bearing = math.radians(bearing_deg)
    if abs(bearing) < 1e-9:
        along = np.arange(SAMPLE_SPACING_M, AIM_M + 1e-9, SAMPLE_SPACING_M)
        return np.column_stack([along, np.zeros_like(along)])
    radius = AIM_M / (2 * math.sin(bearing))  # negative for a path to the right
    turned = np.arange(SAMPLE_SPACING_M, radius * 2 * bearing + 1e-9, SAMPLE_SPACING_M) / radius
    return radius * np.column_stack([np.sin(turned), 1 - np.cos(turned)])


def fan(
    obstacles: Sequence[Obstacle], setup: ChairSetup, margin_m: float, stop_m: float = 0.0
) -> dict[str, np.ndarray]:
    """The paths the chair fits along, by number.

    Each is cut ``stop_m`` short of where the chair's body would come within ``margin_m``
    of an obstacle, and where it leaves the camera's view, since nothing is sensed out
    there. ``stop_m`` is footprint.stop_distance_m at the speed it will be driven, plus
    PLAN_SLACK_M: the runner stops by the same rule on what is left of the path, so a path
    that was offered is driven to its end unless something has changed.
    """
    shape = Body(setup.chair.width_m, setup.chair.length_m, margin_m)
    paths = {}
    for n, bearing in enumerate(BEARINGS_DEG, start=1):
        path = arc(bearing)
        ahead, left = (path - setup.lens_xy).T
        aside = np.abs(np.degrees(np.arctan2(left, ahead)))
        unseen = (ahead > NEAR_M) & (aside > setup.camera.hfov_deg / 2 - VIEW_EDGE_DEG)
        clear = int(np.argmax(unseen)) if unseen.any() else len(path)
        hit = first_hit(path, obstacles, shape)
        if hit is not None:
            clear = min(clear, round((hit[0] - stop_m) / SAMPLE_SPACING_M) - 1)
        if clear * SAMPLE_SPACING_M >= MIN_M:
            paths[str(n)] = path[:clear]
    return paths


def options(paths: dict[str, np.ndarray]) -> dict[str, str]:
    """Everything that can be picked, in words: the paths on offer, then the rest."""
    listed = {}
    for label, path in paths.items():
        bearing = BEARINGS_DEG[int(label) - 1]
        side = "left" if bearing > 0 else "right"
        way = f"curving {side} toward {abs(bearing)}° {side}" if bearing else "straight ahead"
        whole = len(path) == len(arc(bearing))
        ends = "clear to its end" if whole else "then blocked or out of view"
        listed[label] = f"path {label}: {way}, {len(path) * SAMPLE_SPACING_M:.1f} m, {ends}"
    return {**listed, **OTHERS}


def _in_view(pixels: np.ndarray, width: int, height: int) -> np.ndarray:
    """The pixels that are in the picture: not behind the lens, far below it, or off a side.

    A little below the bottom edge is kept, so a line starts at the edge and not at the
    first point above it.
    """
    x, y = pixels.T
    return pixels[~np.isnan(x) & (x >= 0) & (x < width) & (y < height * 1.3)]


def _guides(pen: ImageDraw.ImageDraw, setup: ChairSetup, size: tuple[int, int]) -> None:
    """Thin lines where the chair's sides pass if it drives straight, with distance marks."""
    front, half = setup.chair.length_m / 2, setup.chair.width_m / 2
    ahead = front + np.arange(0.0, MARKS_M[-1] + 0.5 + 1e-9, SAMPLE_SPACING_M)
    for side in (half, -half):
        rail = _in_view(
            setup.to_pixels(np.column_stack([ahead, np.full_like(ahead, side)]), *size), *size
        )
        if len(rail) > 1:
            pen.line([tuple(p) for p in rail], fill=GUIDE, width=2)
    font = ImageFont.load_default(size=round(size[0] * 0.017))
    for mark in MARKS_M:
        ends = setup.to_pixels([[front + mark, half], [front + mark, -half]], *size)
        if len(_in_view(ends, *size)) == 2:
            pen.line([tuple(p) for p in ends], fill=GUIDE, width=2)
            pen.text(
                (ends[0][0] - 6, ends[0][1]),
                f"{mark:g} m",
                fill=(255, 255, 255, 230),
                font=font,
                anchor="rm",
                stroke_width=2,
                stroke_fill=(0, 0, 0, 160),
            )


def draw(jpeg: bytes, paths: dict[str, np.ndarray], setup: ChairSetup) -> bytes:
    """The camera's JPEG with the guide lines and each path on the floor, numbered.

    A path is solid as far as the chair's front gets along it, and faintly dashed from there
    to its end, so it shows both how far the chair can go and where that way leads. Its
    number is at its far end, or where it leaves the picture. With no paths, only the guide
    lines are drawn.
    """
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if image.width < DRAW_WIDTH:
        scale = DRAW_WIDTH / image.width
        image = image.resize((DRAW_WIDTH, round(image.height * scale)), Image.Resampling.LANCZOS)
    width, height = image.size
    pen = ImageDraw.Draw(image, "RGBA")
    _guides(pen, setup, image.size)
    radius = round(width * 0.022)
    font = ImageFont.load_default(size=round(radius * 1.5))
    front = round(setup.chair.length_m / 2 / SAMPLE_SPACING_M)
    lines = []
    for label, path in paths.items():
        whole = arc(BEARINGS_DEG[int(label) - 1])
        reach = min(len(whole), len(path) + front)  # the chair's front is ahead of its middle
        solid = _in_view(setup.to_pixels(whole[:reach], width, height), width, height)
        rest = _in_view(setup.to_pixels(whole[reach - 1 :], width, height), width, height)
        colour = COLOURS[int(label) - 1]
        if len(solid) > 1:
            line = [tuple(p) for p in solid]
            pen.line(line, fill="black", width=round(width * 0.009), joint="curve")
            pen.line(line, fill=colour, width=round(width * 0.005), joint="curve")
        for a, b in zip(rest[:-1:2], rest[1::2], strict=False):  # every other piece: dashes
            pen.line([tuple(a), tuple(b)], fill=colour + "b0", width=round(width * 0.004))
        if len(solid) + len(rest):
            lines.append((label, colour, np.vstack([solid, rest])))
    placed: list[tuple[float, float]] = []
    for label, colour, line in lines:  # numbers last, so no line runs over one
        # At the far end; moved back along its own line until it is clear of the others.
        for x, y in line[::-1]:
            x = min(max(x, radius + 2), width - radius - 2)
            y = min(max(y, radius + 2), height - radius - 2)
            if all(math.hypot(x - px, y - py) > 2.2 * radius for px, py in placed):
                break
        placed.append((x, y))
        pen.ellipse(
            [x - radius, y - radius, x + radius, y + radius], fill=colour, outline="black", width=3
        )
        pen.text((x, y), label, fill="black", font=font, anchor="mm")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()
