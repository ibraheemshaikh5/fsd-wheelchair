"""See what is in the chair's way, from the one camera.

The driving model returns a path and nothing else, so it cannot be asked what it
is avoiding. This is a second model beside it: a depth model turns the picture into
distances, the floor is found in that, and whatever stands up from the floor becomes
an obstacle on the chair's map (metres ahead, metres left).

    obstacles = DepthObstacles().find(frame, camera)

Two things keep it honest with a camera that has not been measured exactly:

- the floor is fitted from the picture itself, so the camera's tilt is not needed;
- the fitted floor is known to be the camera's height below the lens, which sets the scale.

Only what the camera sees in the current frame counts: nothing is remembered once it
leaves the picture. It is optional: it needs the ``detect`` dependency group (torch), and
the console runs without it when that is not installed.
"""

from __future__ import annotations

import importlib.util
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from fsd_ml.pipeline.types import Obstacle

MODEL_ID = "depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf"
# The depth map is worked on at this size: plenty for a map with 15 cm cells.
GRID = (96, 54)
CELL_M = 0.15
# Only what is this close matters to a chair moving at walking pace.
RANGE_M = 3.5
# Lower than this is the floor (the allowance grows with distance, where depth is rougher);
# higher than the top is something the chair passes under.
FLOOR_BAND_M = 0.07
FLOOR_BAND_PER_M = 0.04
TOP_M = 1.3
# A cell needs this many points standing in it to count: one stray point is noise.
MIN_POINTS = 3
KEEP = 60
# The bottom of the picture shows the chair's own footplate and caster: never an obstacle.
OWN_CHAIR_ROWS = 0.12
# Where the chair's own front corner sits in the picture: below and to the right of this
# point (fractions down, across). It depends on where the camera is mounted.
OWN_CHAIR_CORNER = (0.72, 0.82)
# The outermost columns on each side are not used.
EDGE_COLS = 0.03


def available() -> bool:
    """Whether the depth model's libraries are installed."""
    return all(importlib.util.find_spec(name) for name in ("torch", "transformers"))


def rays(width: int, height: int, hfov_deg: float) -> np.ndarray:
    """A unit-depth ray for each pixel: (right, down, forward), shape (height, width, 3)."""
    focal = (width / 2) / math.tan(math.radians(hfov_deg) / 2)
    xs = (np.arange(width) + 0.5 - width / 2) / focal
    ys = (np.arange(height) + 0.5 - height / 2) / focal
    grid_x, grid_y = np.meshgrid(xs, ys)
    return np.dstack([grid_x, grid_y, np.ones_like(grid_x)])


# With the camera's tilt known: how far the floor found may lean from it, and how much of the
# picture (from the bottom) is searched for it.
MAX_LEAN_DEG = 5.0
FLOOR_SEARCH_ROWS = 0.3


def floor_by_tilt(points: np.ndarray, tilt_down_deg: float) -> tuple[np.ndarray, float] | None:
    """The floor, for a camera whose tilt is known (ml/chair.json): the lowest level surface.

    A plane fitted freely to the bottom of the picture is tipped by whatever fills it: beside
    a wall, or close behind a chest, the "floor" comes out leaning 10 degrees and every
    distance with it. But the camera is fixed to the chair, so which way is down is known to
    a few degrees. Measured along that, the floor is the lowest level the picture shows much
    of. A plane is then fitted to the points at that level, and kept only if it leans less
    than MAX_LEAN_DEG from the known tilt (the chair pitches a little as it moves).
    """
    tilt = math.radians(tilt_down_deg)
    down = np.array([0.0, math.cos(tilt), math.sin(tilt)])
    low = points[int(points.shape[0] * (1 - FLOOR_SEARCH_ROWS)) :].reshape(-1, 3)
    low = low[(low[:, 2] > 0.1) & (low[:, 2] < 6.0)]
    below = low @ down
    low, below = low[below > 0.02], below[below > 0.02]
    if len(low) < 50:
        return None
    # Levels 4% apart; the floor is the lowest of those nearly as full as the fullest.
    step = 0.04 * float(np.median(below))
    counts = np.bincount((below / step).astype(int))
    full = np.nonzero(counts >= 0.5 * counts.max())[0]
    level = (full[-1] + 0.5) * step
    on_it = low[np.abs(below - level) < 1.5 * step]
    if len(on_it) < 50:
        return None
    centre = on_it.mean(axis=0)
    _, _, axes = np.linalg.svd(on_it - centre, full_matrices=False)
    normal = axes[2] if axes[2][1] > 0 else -axes[2]
    if math.degrees(math.acos(min(1.0, float(normal @ down)))) > MAX_LEAN_DEG:
        normal = down
    return normal, float(normal @ centre)


def fit_floor(
    points: np.ndarray, tilt_down_deg: float | None = None
) -> tuple[np.ndarray, float] | None:
    """The floor as a plane ``normal . p = distance`` below the lens, from the lowest points.

    With the camera's tilt known, see ``floor_by_tilt``. Without it: starts from the bottom
    of the picture, which is nearly always floor, and refits twice on the points that agree,
    so a box or a foot in that region does not tip the plane.
    """
    if tilt_down_deg is not None:
        return floor_by_tilt(points, tilt_down_deg)
    height = points.shape[0]
    low = points[int(height * 0.72) :].reshape(-1, 3)
    low = low[(low[:, 2] > 0.1) & (low[:, 2] < 6.0)]
    if len(low) < 50:
        return None
    chosen = low
    normal, distance = np.array([0.0, 1.0, 0.0]), 0.0
    for _ in range(3):
        centre = chosen.mean(axis=0)
        _, _, axes = np.linalg.svd(chosen - centre, full_matrices=False)
        normal = axes[2] if axes[2][1] > 0 else -axes[2]  # pointing down, toward the floor
        distance = float(normal @ centre)
        off = np.abs(low @ normal - distance)
        keep = low[off < max(0.03, float(np.percentile(off, 60)))]
        if len(keep) < 50:
            break
        chosen = keep
    # A floor is below the camera and roughly level with it; anything else is a wall or a mistake.
    if distance <= 0.05 or normal[1] < 0.75:
        return None
    return normal, distance


@dataclass(frozen=True, slots=True)
class Ground:
    """Where every cell of a depth map is, in the chair's frame: metres ahead, metres left,
    and metres above the floor fitted in that picture. Each array is (rows, columns)."""

    ahead: np.ndarray
    left: np.ndarray
    above: np.ndarray
    # For the page: the depth scale and the camera's tilt as fitted.
    info: dict[str, Any]

    def floor(self) -> np.ndarray:
        """Which cells are floor the chair could drive on: low enough, and near enough to trust."""
        low = self.above < FLOOR_BAND_M + FLOOR_BAND_PER_M * np.maximum(self.ahead, 0.0)
        return low & (self.ahead > 0.15) & (self.ahead < RANGE_M + 0.5)

    def at(self, across: float, down: float) -> tuple[float, float, bool]:
        """The cell at a point of the picture (fractions across and down, 0 to 1): metres
        ahead and left of the chair's middle, and whether it is floor."""
        rows, columns = self.ahead.shape
        row = min(rows - 1, max(0, int(down * rows)))
        column = min(columns - 1, max(0, int(across * columns)))
        return (
            float(self.ahead[row, column]),
            float(self.left[row, column]),
            bool(self.floor()[row, column]),
        )


def ground_from_depth(
    depth: np.ndarray,
    hfov_deg: float,
    camera_height_m: float,
    lens_xy: tuple[float, float] = (0.0, 0.0),
    tilt_down_deg: float | None = None,
) -> Ground | None:
    """Every cell of a depth map placed in the chair's frame, or None if no floor is found.

    ``tilt_down_deg`` is the camera's tilt if it has been measured (see ``floor_by_tilt``).
    """
    height, width = depth.shape
    points = rays(width, height, hfov_deg) * depth[:, :, None]
    floor = fit_floor(points, tilt_down_deg)
    if floor is None:
        return None
    normal, distance = floor
    scale = camera_height_m / distance  # the fitted floor is the lens height away, by definition
    # Forward along the floor: the camera's axis with its downward part taken out.
    forward = np.array([0.0, 0.0, 1.0]) - normal * normal[2]
    forward /= np.linalg.norm(forward)
    right = np.cross(normal, forward)  # down x forward = right
    tilt = math.degrees(math.asin(max(-1.0, min(1.0, float(normal[2])))))
    return Ground(
        ahead=(points @ forward) * scale + lens_xy[0],
        left=-(points @ right) * scale + lens_xy[1],
        above=(distance - points @ normal) * scale,
        info={"floor": True, "scale": round(scale, 3), "tilt_deg": round(tilt, 1)},
    )


def obstacles_from_depth(
    depth: np.ndarray,
    hfov_deg: float,
    camera_height_m: float,
    lens_xy: tuple[float, float] = (0.0, 0.0),
    tilt_down_deg: float | None = None,
) -> tuple[list[Obstacle], dict[str, Any]]:
    """What stands on the floor, in the chair's frame, from a depth map in any consistent unit."""
    height, width = depth.shape
    ground = ground_from_depth(depth, hfov_deg, camera_height_m, lens_xy, tilt_down_deg)
    if ground is None:
        return [], {"floor": False}
    ahead, left, above = ground.ahead - lens_xy[0], ground.left - lens_xy[1], ground.above
    standing = (
        (above > FLOOR_BAND_M + FLOOR_BAND_PER_M * ahead)
        & (above < TOP_M)
        & (ahead > 0.15)
        & (ahead < RANGE_M)
    )
    standing[int(height * (1 - OWN_CHAIR_ROWS)) :] = False
    # The chair's own front corner shows at the bottom right, and the depth model smears it
    # upward into the floor beside it; the very edges of a wide lens also bend the floor up.
    # Neither is something in the way.
    corner_row, corner_col = OWN_CHAIR_CORNER
    standing[int(height * corner_row) :, int(width * corner_col) :] = False
    edge = max(1, round(width * EDGE_COLS))
    standing[:, :edge] = False
    standing[:, -edge:] = False
    # Each cell reports where its points really are, not the cell's middle.
    cells: dict[tuple[int, int], list[float]] = {}
    for a, b in zip(ahead[standing], left[standing], strict=True):
        cell = cells.setdefault((int(a // CELL_M), int(b // CELL_M)), [0, 0.0, 0.0])
        cell[0] += 1
        cell[1] += a
        cell[2] += b
    filled = sorted((c for c in cells.values() if c[0] >= MIN_POINTS), key=lambda c: c[1] / c[0])
    obstacles = [
        Obstacle(
            f"o{i}",
            "object",
            round(float(total_a / n) + lens_xy[0], 3),
            round(float(total_b / n) + lens_xy[1], 3),
            radius_m=CELL_M * 0.6,
        )
        for i, (n, total_a, total_b) in enumerate(filled[:KEEP])
    ]
    return obstacles, ground.info


class DepthObstacles:
    """Depth Anything V2 (small, indoor), then the floor-and-obstacle arithmetic above."""

    def __init__(self, model_id: str = MODEL_ID) -> None:
        self.model_id = model_id
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self.device = "cpu"
        # One picture at a time: the watcher and a pilot's look may both ask.
        self._lock = threading.Lock()

    def depth(self, image: np.ndarray) -> np.ndarray:
        with self._lock:
            return self._depth(image)

    def _depth(self, image: np.ndarray) -> np.ndarray:
        if self._model is None:
            import torch
            from transformers import AutoImageProcessor, AutoModelForDepthEstimation

            self.device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._processor = AutoImageProcessor.from_pretrained(self.model_id)
            self._model = AutoModelForDepthEstimation.from_pretrained(self.model_id)
            self._model = self._model.to(self.device).eval()
            self._torch = torch
        torch = self._torch
        inputs = self._processor(images=image, return_tensors="pt").to(self.device)
        with torch.no_grad():
            predicted = self._model(**inputs).predicted_depth
        small = torch.nn.functional.interpolate(
            predicted[None], size=(GRID[1], GRID[0]), mode="bilinear", align_corners=False
        )
        return small[0, 0].float().cpu().numpy()

    def find(
        self,
        image: np.ndarray,
        hfov_deg: float,
        camera_height_m: float,
        lens_xy: tuple[float, float] = (0.0, 0.0),
        tilt_down_deg: float | None = None,
    ) -> tuple[list[Obstacle], dict[str, Any]]:
        return obstacles_from_depth(
            self.depth(image), hfov_deg, camera_height_m, lens_xy, tilt_down_deg
        )

    def ground(
        self,
        image: np.ndarray,
        hfov_deg: float,
        camera_height_m: float,
        lens_xy: tuple[float, float] = (0.0, 0.0),
        tilt_down_deg: float | None = None,
    ) -> Ground | None:
        return ground_from_depth(
            self.depth(image), hfov_deg, camera_height_m, lens_xy, tilt_down_deg
        )


class ObstacleWatcher:
    """Looks for obstacles in the newest frame, in the background, so driving never waits on it.

    ``look`` gives the newest frame and how long ago it was taken, or None when there is
    none fresh enough; ``find`` turns a frame into obstacles. What is published is always
    from one frame, the latest looked at, and ``age_s`` says how old that picture is now:
    a moving chair is that much closer to everything in it.
    """

    def __init__(
        self,
        look: Callable[[], tuple[np.ndarray, float] | None],
        find: Callable[[np.ndarray], tuple[list[Obstacle], dict[str, Any]]],
        period_s: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._look, self._find, self._period_s = look, find, period_s
        self._clock = clock
        self._stop = threading.Event()
        self.obstacles: tuple[Obstacle, ...] = ()
        # For the page: floor, scale, how long the look took; or the problem.
        self.info: dict[str, Any] = {}
        # When the picture behind ``obstacles`` was taken.
        self.at_s = 0.0

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def age_s(self) -> float:
        return self._clock() - self.at_s

    def fresh(self, within_s: float = 1.0) -> tuple[Obstacle, ...] | None:
        """What stands in view, or None when the floor has not been seen recently."""
        if self.age_s() > within_s or not self.info.get("floor"):
            return None
        return self.obstacles

    def once(self) -> None:
        now = self._clock()
        looked = self._look()
        if looked is None:
            return
        image, age_s = looked
        started = time.perf_counter()
        found, info = self._find(image)
        self.obstacles = tuple(found)
        self.info = {**info, "ms": round((time.perf_counter() - started) * 1000.0, 1)}
        self.at_s = now - age_s

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.once()
            except Exception as error:  # noqa: BLE001  a broken depth model must not stop the console
                self.info = {"problem": str(error)}
                self._stop.wait(3.0)
            self._stop.wait(self._period_s)
