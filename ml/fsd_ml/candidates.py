"""The ways the chair could go next, drawn on the camera's picture for a model to pick from.

A model that sees is poor at judging metres and degrees, and good at pointing. So it is not
asked for a distance or an angle: a fan of paths the chair is known to fit along is drawn on
the floor of the picture, each with a number at its end, and it answers with a number.

    paths = fan(obstacles, shape, stop_m)    # {"1": path, ... "7": path}, blocked ones left out
    picture = draw(jpeg, paths, setup, tilt)  # the JPEG with the paths and their numbers on it
    listed = options(paths)                  # {"1": "path 1: 2.4 m, curving left", ..., "L": ...}

Paths are for the middle of the chair, in its own frame (metres ahead, metres left), with a
point every 0.1 m. Number 1 is always the leftmost and 4 is always straight ahead, whichever
are on offer.
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

# How far each path has turned by its end, left to right as numbered.
HEADINGS_DEG = (45, 30, 15, 0, -15, -30, -45)
MAX_M = 3.0
# A path with less clear floor than this is not offered.
MIN_M = 0.5
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


def arc(heading_deg: float, length_m: float = MAX_M) -> np.ndarray:
    """A path of constant curve that has turned ``heading_deg`` to the left over MAX_M."""
    along = np.arange(SAMPLE_SPACING_M, length_m + 1e-9, SAMPLE_SPACING_M)
    curve = math.radians(heading_deg) / MAX_M
    if abs(curve) < 1e-9:
        return np.column_stack([along, np.zeros_like(along)])
    return np.column_stack([np.sin(curve * along), 1 - np.cos(curve * along)]) / curve


def fan(obstacles: Sequence[Obstacle], shape: Body, stop_m: float = 0.0) -> dict[str, np.ndarray]:
    """The paths the chair fits along, by number, each cut ``stop_m`` short of what it meets."""
    paths = {}
    for n, heading in enumerate(HEADINGS_DEG, start=1):
        path = arc(heading)
        hit = first_hit(path, obstacles, shape)
        clear = MAX_M if hit is None else hit[0] - stop_m
        if clear >= MIN_M:
            paths[str(n)] = path[: round(min(clear, MAX_M) / SAMPLE_SPACING_M)]
    return paths


def options(paths: dict[str, np.ndarray]) -> dict[str, str]:
    """Everything that can be picked, in words: the paths on offer, then the rest."""
    listed = {}
    for label, path in paths.items():
        heading = HEADINGS_DEG[int(label) - 1]
        way = "straight ahead" if heading == 0 else f"curving {'left' if heading > 0 else 'right'}"
        listed[label] = f"path {label}: {len(path) * SAMPLE_SPACING_M:.1f} m, {way}"
    return {**listed, **OTHERS}


def draw(
    jpeg: bytes, paths: dict[str, np.ndarray], setup: ChairSetup, tilt_down_deg: float | None = None
) -> bytes:
    """The camera's JPEG with each path as a line on the floor and its number at its end.

    ``tilt_down_deg`` is the camera's tilt as the obstacle sense fitted it in this picture, if
    it did; without it the saved tilt (ml/chair.json) is used.
    """
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if image.width < DRAW_WIDTH:
        scale = DRAW_WIDTH / image.width
        image = image.resize((DRAW_WIDTH, round(image.height * scale)), Image.Resampling.LANCZOS)
    width, height = image.size
    pen = ImageDraw.Draw(image)
    radius = round(width * 0.02)
    font = ImageFont.load_default(size=round(radius * 1.5))
    reach = np.arange(SAMPLE_SPACING_M, setup.chair.length_m / 2 + 1e-9, SAMPLE_SPACING_M)
    ends = []
    for label, path in paths.items():
        # The path is where the chair's middle goes; the line runs on to where its front ends
        # up, so the number sits on the floor the chair will have reached.
        along = path[-1] - (path[-2] if len(path) > 1 else 0.0)
        line_xy = np.vstack([path, path[-1] + np.outer(reach, along / np.linalg.norm(along))])
        pixels = setup.to_pixels(line_xy, width, height, tilt_down_deg)
        # Only the part the camera can see: the floor just in front of the chair is below it.
        seen = pixels[~np.isnan(pixels).any(axis=1) & (pixels[:, 1] < height)]
        if not len(seen):
            continue
        colour = COLOURS[(int(label) - 1) % len(COLOURS)]
        line = [tuple(p) for p in seen]
        pen.line(line, fill="black", width=round(width * 0.009), joint="curve")
        pen.line(line, fill=colour, width=round(width * 0.005), joint="curve")
        ends.append((label, colour, *seen[-1]))
    last_x = -math.inf
    for label, colour, x, y in sorted(ends, key=lambda end: end[2]):  # numbers over the lines
        # Kept inside the picture, and off the number to its left.
        x = min(max(x, radius + 2, last_x + 2 * radius), width - radius - 2)
        y = min(max(y - radius, radius + 2), height - radius - 2)
        last_x = x
        pen.ellipse(
            [x - radius, y - radius, x + radius, y + radius], fill=colour, outline="black", width=3
        )
        pen.text((x, y), label, fill="black", font=font, anchor="mm")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()
