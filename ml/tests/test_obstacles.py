"""Obstacles from a depth map, and the ways round them."""

import numpy as np

from fsd_ml.drivers import Candidate
from fsd_ml.obstacles import arc, obstacles_from_depth, rays, ways_round
from fsd_ml.pipeline.footprint import ChairShape, FootprintConfig, choose

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


def test_the_chair_takes_the_nearest_clear_way_round():
    found, _ = obstacles_from_depth(room(box_left_m=0.0), HFOV, HEIGHT_M)
    ways = ways_round(0.5, 0.0)  # the model wants to go straight, into the box
    shape = ChairShape(0.58, 1.02, 0.1)
    config = FootprintConfig(shape=shape, caution_m=0.1, slow_distance_m=2.0, stop_distance_m=0.5)
    picked = choose([Candidate(path, score) for _, path, score in ways], found, config)
    assert picked.index not in (None, 0) and picked.status != "blocked"
    assert abs(ways[picked.index][0]) > 0.2  # it bends, one way or the other
    # With nothing there it keeps the model's own heading.
    assert choose([Candidate(path, score) for _, path, score in ways], [], config).index == 0


def test_an_arc_bends_left_for_a_positive_bend():
    path = arc(0.8)
    assert path[-1][1] > 0.5 and arc(0.0)[-1][1] == 0.0


def test_the_watcher_remembers_through_a_covered_camera_and_can_be_made_to_forget():
    from fsd_ml.obstacles import ObstacleWatcher
    from fsd_ml.pipeline.types import Obstacle

    now, speed = [0.0], [0.0]
    thing = Obstacle("o", "object", 2.0, 1.6, 0.09)
    looks = {"found": [thing], "floor": True}
    watcher = ObstacleWatcher(
        lambda: np.zeros((4, 4, 3), np.uint8),
        lambda image: (looks["found"], {"floor": looks["floor"]}),
        motion=lambda: (speed[0], 0.0),
        clock=lambda: now[0],
    )
    for _ in range(3):
        now[0] += 0.1
        watcher.once()
    assert watcher.info["seen"] == 1
    # The camera is covered while the chair is driven a metre: nothing is cleared, the map moves.
    looks.update(found=[], floor=False)
    speed[0] = 0.5
    for _ in range(20):
        now[0] += 0.1
        watcher.once()
    (kept,) = watcher.obstacles
    assert abs(kept.x - 1.0) < 0.06 and watcher.info["remembered"] == 1
    assert watcher.fresh() is not None
    watcher.forget("asked")
    assert watcher.obstacles == ()


def test_a_boxed_thing_is_placed_on_the_floor_and_steered_toward():
    from fsd_ml.obstacles import locate_box, toward

    depth = room(box_left_m=0.3)
    ray = rays(96, 54, HFOV)
    # A loose box in the picture around where the thing 1.5 m ahead, 0.3 m left appears.
    cols = np.where(np.abs(-ray[30, :, 0] * 1.5 - 0.3) < 0.3)[0]
    box = (cols.min() / 96, 0.35, (cols.max() + 1) / 96, 0.8)
    ahead, left = locate_box(depth, HFOV, HEIGHT_M, box)
    assert abs(ahead - 1.5) < 0.15 and abs(left - 0.3) < 0.15
    speed, turn = toward(ahead, left, 0.5)
    assert speed == 0.5 and turn > 0  # it is to the left, so the chair curves left
    assert toward(-0.2, -1.0, 0.5) == (0.0, -0.6)  # beside the chair: turn to face it first
    assert locate_box(np.full((54, 96), 0.3), HFOV, HEIGHT_M, box) is None
