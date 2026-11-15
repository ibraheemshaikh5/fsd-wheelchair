"""The paths offered to a model are ones the chair fits along, drawn where the camera sees them."""

import numpy as np
import pytest

from fsd_ml import candidates
from fsd_ml.chair import Body, Camera, ChairSetup
from fsd_ml.pipeline.footprint import first_hit
from fsd_ml.pipeline.types import Obstacle


def test_paths_fan_out_and_are_cut_short_of_what_is_in_the_way():
    setup = ChairSetup()
    # Every whole path ends 2.5 m from the chair's middle, in its own direction.
    for bearing in candidates.BEARINGS_DEG:
        ahead, left = candidates.arc(bearing)[-1]
        assert np.hypot(ahead, left) == pytest.approx(2.5, abs=0.06)
        assert np.degrees(np.arctan2(left, ahead)) == pytest.approx(bearing, abs=1.5)
    # With nothing in the way, only the camera's view cuts a path: the outer ones leave it.
    open_floor = candidates.fan((), setup, 0.25)
    assert list(open_floor) == list("1234567") and len(open_floor["4"]) == 25
    assert len(open_floor["1"]) < len(candidates.arc(60))
    # A box 2 m ahead: the straight path stops short of it and the chair fits along what is
    # offered; the same box close in front leaves nothing straight on.
    box = [Obstacle("box", "object", 2.0, 0.0, radius_m=0.1)]
    paths = candidates.fan(box, setup, 0.25, stop_m=0.7)
    assert 0.5 <= len(paths["4"]) * 0.1 < 1.0
    shape = Body(setup.chair.width_m, setup.chair.length_m, 0.25)
    assert all(first_hit(path, box, shape) is None for path in paths.values())
    assert "4" not in candidates.fan([Obstacle("box", "object", 1.2, 0.0)], setup, 0.25, 0.7)


def test_floor_points_land_where_a_level_camera_sees_them():
    setup = ChairSetup(Camera(height_m=0.3, tilt_down_deg=0.0, left_m=0.2, from_rear_m=0.9))
    ahead, left = setup.lens_xy
    # 3 m in front of the lens: on the picture's centre line, below its middle by f * h / d.
    (x, y), (x_left, _), behind = setup.to_pixels(
        [[ahead + 3.0, left], [ahead + 3.0, left + 1.0], [ahead - 1.0, left]], 960, 540
    )
    focal = 480 / np.tan(np.radians(40))
    assert np.allclose([x, y], [480, 270 + focal * 0.3 / 3.0])
    assert x_left < x and np.isnan(behind).all()
