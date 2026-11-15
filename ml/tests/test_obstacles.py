"""Obstacles from a depth map."""

import numpy as np

from fsd_ml.obstacles import obstacles_from_depth, rays

HEIGHT_M, HFOV = 0.34, 80.0


def room(box_left_m: float | None = 0.0, scale: float = 1.0) -> np.ndarray:
    """Depth for a level camera over a flat floor, a far wall, and maybe a box 1.5 m ahead."""
    ray = rays(96, 54, HFOV)
    right, down = ray[:, :, 0], ray[:, :, 1]
    depth = np.full(right.shape, 8.0)
    floor = down > 0.02
    depth[floor] = np.minimum(8.0, HEIGHT_M / down[floor])
    if box_left_m is not None:
        side = np.abs(-right * 1.5 - box_left_m) < 0.25
        tall = (HEIGHT_M - down * 1.5 > 0.0) & (HEIGHT_M - down * 1.5 < 0.6)
        depth[side & tall & (depth > 1.5)] = 1.5
    return depth * scale


def test_a_box_on_the_floor_is_found_where_it_stands():
    found, info = obstacles_from_depth(room(box_left_m=0.3), HFOV, HEIGHT_M)
    assert info["floor"] and found
    assert all(1.3 < o.x < 1.8 and 0.0 < o.y < 0.65 for o in found)


def test_an_empty_floor_has_no_obstacles_and_scale_does_not_matter():
    assert obstacles_from_depth(room(box_left_m=None), HFOV, HEIGHT_M)[0] == []
    # A depth model that reads everything 1.7 times too far gives the same map.
    stretched, info = obstacles_from_depth(room(box_left_m=0.3, scale=1.7), HFOV, HEIGHT_M)
    assert abs(info["scale"] - 1 / 1.7) < 0.03
    assert all(1.3 < o.x < 1.8 for o in stretched)


def test_a_covered_camera_reports_no_floor():
    assert obstacles_from_depth(np.full((54, 96), 0.3), HFOV, HEIGHT_M) == ([], {"floor": False})
