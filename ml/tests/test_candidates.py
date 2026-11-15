"""The paths offered to a model are ones the chair fits along, drawn where the camera sees them."""

import numpy as np

from fsd_ml import candidates
from fsd_ml.chair import Body, Camera, ChairSetup
from fsd_ml.pipeline.footprint import first_hit
from fsd_ml.pipeline.types import Obstacle


def test_paths_are_cut_short_of_what_is_in_the_way_and_dropped_when_too_short():
    shape = Body(margin_m=0.25)
    assert [len(p) for p in candidates.fan((), shape).values()] == [30] * 7
    # A box 2.5 m ahead and a little to the right: in the way of every path but the leftmost.
    box = [Obstacle("box", "object", 2.5, -0.4, radius_m=0.1)]
    paths = candidates.fan(box, shape, stop_m=0.7)
    assert len(paths["1"]) == 30 and 5 <= len(paths["4"]) < 12
    assert all(first_hit(path, box, shape) is None for path in paths.values())
    # The same box close in front leaves nothing to offer.
    assert candidates.fan([Obstacle("box", "object", 1.2, 0.0)], shape, stop_m=0.7) == {}


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
