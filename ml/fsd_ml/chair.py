"""Where the camera sits on the chair, and how big the chair is.

The driving model is never told any of this: it only gets the picture. These
numbers are for the code around it:

- the model's paths start at the lens, so they are shifted to start at the
  chair's centre before anything checks whether the chair fits;
- the viewer draws paths on the picture through the same camera;
- the footprint check uses the chair's real size.

They live in ``ml/chair.json``. Set them from the tape measure with:

    uv run python -m fsd_ml.chair --height 32 --offset 27 --side left --from-rear 90 \\
        --tilt 3 --wall-width 190 --chair-width 62 --chair-length 105

Lengths on the command line are centimetres.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, replace
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
    width_m: float = 0.584
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

    def as_dict(self) -> dict:
        ahead, left = self.lens_xy
        return {**asdict(self), "lens_ahead_m": round(ahead, 3), "lens_left_m": round(left, 3)}


def fov_from_wall(width_m: float, distance_m: float = 1.0) -> float:
    """Field of view in degrees, from how much wall the picture shows at a distance."""
    return math.degrees(2 * math.atan(width_m / (2 * distance_m)))


def load(path: Path = SETTINGS) -> ChairSetup:
    """The saved setup, or the placeholder one if there is no file yet."""
    if not path.is_file():
        return ChairSetup()
    saved = json.loads(path.read_text())
    return ChairSetup(
        Camera(**saved.get("camera", {})),
        Body(**saved.get("chair", {})),
        bool(saved.get("measured", False)),
    )


def save(setup: ChairSetup, path: Path = SETTINGS) -> None:
    path.write_text(json.dumps(asdict(setup), indent=2) + "\n")


def check(setup: ChairSetup) -> list[str]:
    """Things about the numbers that look like a slip of the tape or a wrong unit."""
    camera, chair, doubts = setup.camera, setup.chair, []
    if not 0.05 <= camera.height_m <= 2.0:
        doubts.append(f"a lens {camera.height_m:.2f} m off the floor")
    if abs(camera.left_m) > chair.width_m / 2 + 0.15:
        doubts.append("a camera well outside the chair's width")
    if not 0.0 <= camera.from_rear_m <= chair.length_m + 0.3:
        doubts.append("a camera behind the chair or far ahead of the footplate")
    if not -20.0 <= camera.tilt_down_deg <= 60.0:
        doubts.append(f"a tilt of {camera.tilt_down_deg:g} degrees")
    if not 30.0 <= camera.hfov_deg <= 170.0:
        doubts.append(f"a field of view of {camera.hfov_deg:.0f} degrees")
    if not (0.4 <= chair.width_m <= 1.0 and 0.6 <= chair.length_m <= 1.6):
        doubts.append(f"a chair {chair.width_m:.2f} m by {chair.length_m:.2f} m")
    return doubts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Save the camera and chair measurements.")
    parser.add_argument("--height", type=float, help="floor to lens centre, cm")
    parser.add_argument("--offset", type=float, help="chair centre line to lens, cm")
    parser.add_argument("--side", choices=["left", "right"], help="which side the camera is on")
    parser.add_argument("--from-rear", type=float, help="chair's rearmost point to lens, cm")
    parser.add_argument("--tilt", type=float, help="degrees below level")
    parser.add_argument("--wall-width", type=float, help="wall width in the picture from 1 m, cm")
    parser.add_argument("--fov", type=float, help="field of view in degrees, if you know it")
    parser.add_argument("--chair-width", type=float, help="widest point across, cm")
    parser.add_argument("--chair-length", type=float, help="footplate tip to rearmost point, cm")
    parser.add_argument("--margin", type=float, help="clear space kept around the chair, cm")
    parser.add_argument("--file", type=Path, default=SETTINGS)
    args = parser.parse_args(argv)

    setup = load(args.file)
    camera, chair = setup.camera, setup.chair
    if args.height is not None:
        camera = replace(camera, height_m=args.height / 100)
    if args.offset is not None:
        side = args.side or ("left" if camera.left_m >= 0 else "right")
        camera = replace(camera, left_m=args.offset / 100 * (1 if side == "left" else -1))
    elif args.side is not None:
        camera = replace(camera, left_m=abs(camera.left_m) * (1 if args.side == "left" else -1))
    if args.from_rear is not None:
        camera = replace(camera, from_rear_m=args.from_rear / 100)
    if args.tilt is not None:
        camera = replace(camera, tilt_down_deg=args.tilt)
    if args.wall_width is not None:
        camera = replace(camera, hfov_deg=round(fov_from_wall(args.wall_width / 100), 1))
    if args.fov is not None:
        camera = replace(camera, hfov_deg=args.fov)
    if args.chair_width is not None:
        chair = replace(chair, width_m=args.chair_width / 100)
    if args.chair_length is not None:
        chair = replace(chair, length_m=args.chair_length / 100)
    if args.margin is not None:
        chair = replace(chair, margin_m=args.margin / 100)

    given = any(value is not None for name, value in vars(args).items() if name != "file")
    setup = ChairSetup(camera, chair, setup.measured or given)
    if given:
        save(setup, args.file)

    ahead, left = setup.lens_xy
    side = "left" if left >= 0 else "right"
    print(f"{'saved to' if given else 'read from'} {args.file}")
    print(f"  lens: {camera.height_m:.2f} m up, {abs(left):.2f} m {side} of centre,")
    print(
        f"        {ahead:+.2f} m ahead of the chair's middle, tilted {camera.tilt_down_deg:g}° down"
    )
    print(f"  view: {camera.hfov_deg:.0f}° wide")
    print(
        f"  chair: {chair.width_m:.2f} m wide, {chair.length_m:.2f} m long,"
        f" keeping {chair.margin_m:.2f} m clear all round"
    )
    if not setup.measured:
        print("  these are placeholders: nothing has been measured yet")
    for doubt in check(setup):
        print(f"  check this: {doubt}")


if __name__ == "__main__":
    main()
