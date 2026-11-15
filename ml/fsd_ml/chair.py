"""Where the camera sits on the chair, and how big the chair is.

The driving model is never told any of this: it only gets the picture. These
numbers are for the code around it:

- the model's paths start at the lens, so they are shifted to start at the
  chair's centre before anything checks whether the chair fits;
- the console draws paths on the picture through the same camera;
- the footprint check uses the chair's real size.

They live in ``ml/chair.json``, in metres, measured with the camera mounted and someone
sitting in the chair. Edit that file and restart the console.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

SETTINGS = Path(__file__).resolve().parent.parent / "chair.json"


@dataclass(frozen=True, slots=True)
class Camera:
    # Floor to the centre of the lens.
    height_m: float = 0.32
    # Below level; 0 looks straight ahead.
    tilt_down_deg: float = 3.0
    # From the chair's centre line; negative is to the right.
    left_m: float = 0.27
    # From the chair's rearmost point forward to the lens.
    from_rear_m: float = 0.9
    # Side to side.
    hfov_deg: float = 80.0


@dataclass(frozen=True, slots=True)
class Body:
    # Widest point across, and footplate tip to rearmost point.
    width_m: float = 0.635
    length_m: float = 1.016
    # Kept clear on every side.
    margin_m: float = 0.15


@dataclass(frozen=True, slots=True)
class ChairSetup:
    camera: Camera = Camera()
    chair: Body = Body()
    # False until the numbers have come from a tape measure.
    measured: bool = False

    @property
    def lens_xy(self) -> tuple[float, float]:
        """The lens relative to the middle of the chair: metres ahead, metres left."""
        return self.camera.from_rear_m - self.chair.length_m / 2, self.camera.left_m

    def to_chair(self, path_xy: np.ndarray) -> np.ndarray:
        """A path the model drew from the lens, as a path for the middle of the chair.

        The camera looks straight ahead, so this is a plain shift.
        """
        return np.asarray(path_xy, dtype=np.float64).reshape(-1, 2) + self.lens_xy

    def to_pixels(self, floor_xy: np.ndarray, width: int, height: int) -> np.ndarray:
        """Points on the floor (chair frame) as pixels in the camera's picture.

        Points behind the lens come back as NaN. This is the arithmetic of ``project`` in
        console/page.html (its normal lens), so a path is drawn in the same place by both.
        """
        camera = self.camera
        ahead, left = (np.asarray(floor_xy, dtype=np.float64).reshape(-1, 2) - self.lens_xy).T
        tilt = math.radians(camera.tilt_down_deg)
        depth = ahead * math.cos(tilt) + camera.height_m * math.sin(tilt)
        below = -ahead * math.sin(tilt) + camera.height_m * math.cos(tilt)
        depth = np.where(depth < 0.05, np.nan, depth)
        focal = (width / 2) / math.tan(math.radians(camera.hfov_deg) / 2)
        return np.column_stack(
            [width / 2 - focal * left / depth, height / 2 + focal * below / depth]
        )

    def to_floor(self, pixels_xy: np.ndarray, width: int, height: int) -> np.ndarray:
        """Pixels of the camera's picture as the points of the floor they show (chair frame).

        The other way round from ``to_pixels``. A pixel at or above the horizon shows no
        floor and comes back as NaN.
        """
        camera = self.camera
        x, y = np.asarray(pixels_xy, dtype=np.float64).reshape(-1, 2).T
        focal = (width / 2) / math.tan(math.radians(camera.hfov_deg) / 2)
        across, down = (x - width / 2) / focal, (y - height / 2) / focal
        tilt = math.radians(camera.tilt_down_deg)
        # Along the lens's axis to where the ray meets the floor, the lens's height below it.
        falls = down * math.cos(tilt) + math.sin(tilt)
        depth = np.where(falls > 1e-6, camera.height_m / np.where(falls > 1e-6, falls, 1.0), np.nan)
        ahead = depth * (math.cos(tilt) - down * math.sin(tilt))
        return np.column_stack([ahead, -across * depth]) + self.lens_xy

    def as_dict(self) -> dict:
        ahead, left = self.lens_xy
        return {**asdict(self), "lens_ahead_m": round(ahead, 3), "lens_left_m": round(left, 3)}


def load(path: Path = SETTINGS) -> ChairSetup:
    """The saved setup, or the defaults above if there is no file."""
    if not path.is_file():
        return ChairSetup()
    saved = json.loads(path.read_text())
    return ChairSetup(
        Camera(**saved.get("camera", {})),
        Body(**saved.get("chair", {})),
        bool(saved.get("measured", False)),
    )
