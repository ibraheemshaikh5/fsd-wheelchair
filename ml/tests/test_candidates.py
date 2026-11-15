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
    paths = candidates.fan(box, setup, 0.25)
    assert 1.0 <= len(paths["4"]) * 0.1 < 1.3
    shape = Body(setup.chair.width_m, setup.chair.length_m, 0.25)
    assert all(first_hit(path, box, shape) is None for path in paths.values())
    assert "4" not in candidates.fan([Obstacle("box", "object", 1.2, 0.0)], setup, 0.25)


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


def test_a_drawn_route_is_kept_only_on_floor_and_cut_short_of_what_it_meets():
    from fsd_ml import route_pilot

    setup = ChairSetup()

    def drawn(*floor_xy):
        """A route drawn through these floor points, as fractions of the picture."""
        pixels = setup.to_pixels(np.array(floor_xy), 1000, 1000)
        return route_pilot.Plan("route", points=tuple((x / 1000, y / 1000) for x, y in pixels))

    # A pixel and the floor it shows go both ways.
    there = np.array([[1.0, 0.0], [2.5, -0.8]])
    assert np.allclose(setup.to_floor(setup.to_pixels(there, 960, 540), 960, 540), there)
    # Open floor: the whole route is driven, from the chair's middle, smoothly.
    route = route_pilot.interpret(
        drawn((1.0, 0.2), (2.0, 0.6), (3.0, 0.6)), None, [], setup, 0.2, 0.4
    )
    assert route.said == "" and np.hypot(*(route.path[-1] - (3.0, 0.6))) < 0.15
    # A box on the way: the path ends short of it by the stop distance, and the model is told.
    box = [Obstacle("box", "object", 2.5, 0.6, radius_m=0.1)]
    cut = route_pilot.interpret(
        drawn((1.0, 0.2), (2.0, 0.6), (3.0, 0.6)), None, box, setup, 0.2, 0.4
    )
    assert 0.5 < len(cut.path) * 0.1 < 1.6 and "can follow it for" in cut.said
    assert first_hit(cut.path, box, Body(setup.chair.width_m, setup.chair.length_m, 0.2)) is None
    # A point above the horizon is not floor: the route ends before it.
    sky = route_pilot.Plan("route", points=(*drawn((1.0, 0.0), (2.0, 0.0)).points, (0.5, 0.2)))
    kept = route_pilot.interpret(sky, None, [], setup, 0.2, 0.4)
    assert "point 3" in kept.said and np.hypot(*(kept.path[-1] - (2.0, 0.0))) < 0.15


def test_a_turn_on_the_spot_needs_floor_known_to_be_clear_and_nothing_is_forgotten_by_time():
    from fsd_ml.route_pilot import Remembered

    setup = ChairSetup()
    start = (0.0, 0.0, 0.0)
    memory = Remembered(setup, start)
    # Where a run starts, the chair may turn: the person starting it put it there.
    assert memory.turn_room(start, -30.0, 0.0) == (-30.0, "")
    # It drives 3 m on, seeing open floor ahead all the way and a box that ends up beside it.
    for step in range(31):
        pose = (step * 0.1, 0.0, 0.0)
        box = Obstacle("box", "object", 3.3 - pose[0], -0.75, radius_m=0.09)
        memory.stand(pose)
        memory.see([box] if box.x > 0.8 else [], pose, at_s=step * 0.3, driven_m=step * 0.1)
    there = (3.0, 0.0, 0.0)
    # Long after the box left the picture, a turn toward it is still refused...
    memory.see([], there, at_s=500.0, driven_m=3.0)
    room, why = memory.turn_room(there, -60.0, 3.0)
    assert abs(room) < 30 and "saw earlier" in why
    # ...the turn away from it, over floor seen on the way, is allowed...
    assert memory.turn_room(there, 30.0, 3.0) == (30.0, "")
    # ...and a chair set down somewhere it never looked at may not swing round blind.
    blind = Remembered(setup, start)
    room, why = blind.turn_room((6.0, 4.0, 1.0), 30.0, 0.0)
    assert room == 0 and "not seen" in why


def test_the_goal_is_driven_up_to_and_not_kept_clear_of():
    from fsd_ml import route_pilot

    setup = ChairSetup()
    pixels = setup.to_pixels(np.array([(1.0, 0.0), (2.0, 0.0)]), 1000, 1000)
    plan = route_pilot.Plan(
        "route", points=tuple((x / 1000, y / 1000) for x, y in pixels), stop_short_m=0.5
    )
    # Someone standing 3 m ahead, and something else well off to the side.
    person = [
        Obstacle(f"leg{n}", "object", 3.0, side, radius_m=0.03)
        for n, side in enumerate((-0.1, 0.1))
    ]
    other = [Obstacle("box", "object", 1.5, 1.5, radius_m=0.03)]
    # The route was drawn only 2 m of the way, but ends near the goal: it is carried on, and
    # ends with the chair's front half a metre from the person, as asked.
    to_goal = route_pilot.interpret(plan, None, person + other, setup, 0.08, 0.2, goal=(3.0, 0.0))
    front = to_goal.path[-1][0] + setup.chair.length_m / 2
    assert to_goal.at_goal and 3.0 - front == pytest.approx(0.5 + 0.03, abs=0.15)
    # Asked to stop right in front, it goes nearer than it would to an obstacle in its way.
    close = route_pilot.Plan("route", points=plan.points, stop_short_m=0.15)
    nearer = route_pilot.interpret(close, None, person + other, setup, 0.08, 0.2, goal=(3.0, 0.0))
    plain = route_pilot.interpret(
        route_pilot.Plan("route", points=(*plan.points, (0.5, 0.5))), None, person, setup, 0.08, 0.2
    )
    assert len(nearer.path) > len(to_goal.path) and len(nearer.path) >= len(plain.path)
    parts, rest = route_pilot.goal_parts((3.0, 0.0), person + other)
    assert len(parts) == 2 and rest == other
