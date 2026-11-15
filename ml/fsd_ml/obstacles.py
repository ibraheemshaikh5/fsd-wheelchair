"""See what is in the chair's way, from the one camera.

The driving model returns a path and nothing else, so it cannot be asked what it
is avoiding. This is a second model beside it: a depth model turns the picture into
distances, the floor is found in that, and whatever stands up from the floor becomes
an obstacle on the chair's map (metres ahead, metres left).

    obstacles = DepthObstacles().find(frame, camera)

Two things keep it honest with a camera that has not been measured exactly:

- the floor is fitted from the picture itself, so the camera's tilt is not needed;
- the fitted floor is known to be the camera's height below the lens, which sets the scale.

Needs the ``detect`` dependency group.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from fsd_ml.memory import ObstacleMemory
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
# Ways round: how far each one is looked along, and how much more or less it bends (1/m).
AROUND_M = 2.5
BENDS = (0.0, 0.25, -0.25, 0.5, -0.5, 0.85, -0.85, 1.3, -1.3)
MAX_BEND = 1.6


def rays(width: int, height: int, hfov_deg: float) -> np.ndarray:
    """A unit-depth ray for each pixel: (right, down, forward), shape (height, width, 3)."""
    focal = (width / 2) / math.tan(math.radians(hfov_deg) / 2)
    xs = (np.arange(width) + 0.5 - width / 2) / focal
    ys = (np.arange(height) + 0.5 - height / 2) / focal
    grid_x, grid_y = np.meshgrid(xs, ys)
    return np.dstack([grid_x, grid_y, np.ones_like(grid_x)])


def fit_floor(points: np.ndarray) -> tuple[np.ndarray, float] | None:
    """The floor as a plane ``normal . p = distance`` below the lens, from the lowest points.

    Starts from the bottom of the picture, which is nearly always floor, and refits twice
    on the points that agree, so a box or a foot in that region does not tip the plane.
    """
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


def obstacles_from_depth(
    depth: np.ndarray,
    hfov_deg: float,
    camera_height_m: float,
    lens_xy: tuple[float, float] = (0.0, 0.0),
) -> tuple[list[Obstacle], dict[str, Any]]:
    """What stands on the floor, in the chair's frame, from a depth map in any consistent unit."""
    height, width = depth.shape
    points = rays(width, height, hfov_deg) * depth[:, :, None]
    floor = fit_floor(points)
    if floor is None:
        return [], {"floor": False}
    normal, distance = floor
    scale = camera_height_m / distance  # the fitted floor is the lens height away, by definition
    above = (distance - points @ normal) * scale
    # Forward along the floor: the camera's axis with its downward part taken out.
    forward = np.array([0.0, 0.0, 1.0]) - normal * normal[2]
    forward /= np.linalg.norm(forward)
    right = np.cross(normal, forward)  # down x forward = right
    ahead = (points @ forward) * scale
    left = -(points @ right) * scale
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
    # Each cell reports where its points really are, not the cell's middle: positions that
    # move smoothly as the chair does are what lets the memory follow them.
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
    tilt = math.degrees(math.asin(max(-1.0, min(1.0, float(normal[2])))))
    return obstacles, {"floor": True, "scale": round(scale, 3), "tilt_deg": round(tilt, 1)}


def locate_box(
    depth: np.ndarray,
    hfov_deg: float,
    camera_height_m: float,
    box: tuple[float, float, float, float],
    lens_xy: tuple[float, float] = (0.0, 0.0),
) -> tuple[float, float] | None:
    """Where the thing inside a box in the picture stands: (metres ahead, metres left).

    ``box`` is (x0, y0, x1, y1) as fractions of the picture. The answer is the middle of the
    points inside it that stand up from the floor, so a box drawn a little loose around a
    thing still gives the thing, not the floor around it or the wall behind.
    """
    height, width = depth.shape
    points = rays(width, height, hfov_deg) * depth[:, :, None]
    floor = fit_floor(points)
    if floor is None:
        return None
    normal, distance = floor
    scale = camera_height_m / distance
    forward = np.array([0.0, 0.0, 1.0]) - normal * normal[2]
    forward /= np.linalg.norm(forward)
    right = np.cross(normal, forward)
    c0, c1 = int(box[0] * width), max(int(box[0] * width) + 1, math.ceil(box[2] * width))
    r0, r1 = int(box[1] * height), max(int(box[1] * height) + 1, math.ceil(box[3] * height))
    inside = points[r0:r1, c0:c1].reshape(-1, 3)
    if len(inside) == 0:
        return None
    above = (distance - inside @ normal) * scale
    ahead = (inside @ forward) * scale
    left = -(inside @ right) * scale
    standing = (above > FLOOR_BAND_M) & (ahead > 0.1)
    if standing.sum() >= 2:
        ahead, left = ahead[standing], left[standing]
    # The nearer half: the thing itself, not what shows behind it through the box.
    near = ahead <= np.median(ahead)
    return float(np.median(ahead[near])) + lens_xy[0], float(np.median(left[near])) + lens_xy[1]


def toward(ahead: float, left: float, speed_mps: float) -> tuple[float, float]:
    """The speed and turn rate (positive left) that curve the chair onto a point."""
    distance_sq = ahead * ahead + left * left
    if distance_sq < 1e-6:
        return 0.0, 0.0
    if ahead <= 0.0:  # beside or behind: turn toward it first
        return 0.0, math.copysign(0.6, left)
    return speed_mps, speed_mps * 2.0 * left / distance_sq


def arc(bend: float, length_m: float = AROUND_M, step_m: float = 0.1) -> np.ndarray:
    """A path of constant bend (1/m, positive left) from the chair's middle, shape (n, 2)."""
    along = np.arange(step_m, length_m + 1e-9, step_m)
    if abs(bend) < 1e-3:
        return np.column_stack([along, np.zeros_like(along)])
    along = along[np.abs(bend * along) <= math.pi / 2]
    return np.column_stack([np.sin(bend * along) / bend, (1 - np.cos(bend * along)) / bend])


def ways_round(
    v_mps: float, w_radps: float, length_m: float = AROUND_M
) -> list[tuple[float, np.ndarray, float]]:
    """The model's own heading played forward, and the same bent a little more each way.

    Each is (bend, path, score): the model's own heading scores highest and the score falls
    as a way strays from it, so the chair takes the clear way nearest to what the model wanted.
    """
    wanted = w_radps / v_mps if v_mps > 0.05 else 0.0
    ways = []
    for rank, change in enumerate(BENDS):
        bend = max(-MAX_BEND, min(MAX_BEND, wanted + change))
        ways.append((bend, arc(bend, length_m), 1.0 - 0.08 * rank))
    return ways


class DepthObstacles:
    """Depth Anything V2 (small, indoor), then the floor-and-obstacle arithmetic above."""

    def __init__(self, model_id: str = MODEL_ID) -> None:
        self.model_id = model_id
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self.device = "cpu"
        # The newest depth map, kept so a boxed target can be placed on the floor.
        self.last_depth: np.ndarray | None = None

    def depth(self, image: np.ndarray) -> np.ndarray:
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
        self.last_depth = small[0, 0].float().cpu().numpy()
        return self.last_depth

    def find(
        self,
        image: np.ndarray,
        hfov_deg: float,
        camera_height_m: float,
        lens_xy: tuple[float, float] = (0.0, 0.0),
    ) -> tuple[list[Obstacle], dict[str, Any]]:
        return obstacles_from_depth(self.depth(image), hfov_deg, camera_height_m, lens_xy)


class ObstacleWatcher:
    """Keeps the obstacle map fresh in the background, so driving never waits on it.

    Each pass moves the remembered map by how the chair has just moved, then adds what the
    depth model sees now. When the depth model sees no floor (a covered camera) nothing is
    added or cleared, and the map keeps moving.
    """

    def __init__(
        self,
        look: Callable[[], np.ndarray | None],
        find: Callable[[np.ndarray], tuple[list[Obstacle], dict[str, Any]]],
        memory: ObstacleMemory | None = None,
        motion: Callable[[], tuple[float, float]] | None = None,
        period_s: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._look, self._find, self._period_s = look, find, period_s
        self.memory = memory or ObstacleMemory()
        # The chair's speed (m/s) and turn rate (rad/s, positive left) right now.
        self._motion = motion or (lambda: (0.0, 0.0))
        self._clock = clock
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self.obstacles: tuple[Obstacle, ...] = ()
        self.listed: list[list[float]] = []
        self.info: dict[str, Any] = {}
        self.at_s = 0.0
        self._moved_s: float | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def fresh(self, within_s: float = 1.0) -> tuple[Obstacle, ...] | None:
        """The map, or None when the camera has not been looked through recently."""
        return self.obstacles if self._clock() - self.at_s <= within_s else None

    def forget(self, why: str | None = None) -> None:
        with self._guard:
            self.memory.forget(why)
            self.obstacles, self.listed = (), []

    def once(self) -> None:
        now = self._clock()
        speed, turn = self._motion()
        image = self._look()
        started = time.perf_counter()
        found, info = self._find(image) if image is not None else ([], {})
        with self._guard:
            if self._moved_s is not None:
                self.memory.move(speed, turn, now - self._moved_s)
            self._moved_s = now
            if info.get("floor"):
                self.memory.see(found, now)
            else:
                self.memory.blind()
            self.obstacles = tuple(self.memory.obstacles())
            self.listed = self.memory.listed()
        if image is None:
            return
        self.at_s = now
        self.info = {
            **info,
            "ms": round((time.perf_counter() - started) * 1000.0, 1),
            "seen": sum(1 for o in self.listed if o[3]),
            "remembered": sum(1 for o in self.listed if not o[3]),
            "moving": [round(speed, 2), round(turn, 2)],
            "note": self.memory.note,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.once()
            except Exception as error:  # noqa: BLE001  a broken depth model must not stop the viewer
                self.info = {"problem": str(error)}
                self._stop.wait(3.0)
            self._stop.wait(self._period_s)
