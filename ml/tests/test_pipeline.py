from __future__ import annotations

import math

import numpy as np
import pytest

from fsd_ml.drivers import Candidate
from fsd_ml.geo import world_to_chair
from fsd_ml.pipeline.control import follow
from fsd_ml.pipeline.footprint import choose
from fsd_ml.pipeline.types import Obstacle


def straight(reach: float = 6.0, left: float = 0.0, score: float = 1.0) -> Candidate:
    x = np.linspace(0.25, reach, 24)
    return Candidate(np.column_stack([x, np.full_like(x, left)]), score)


def test_world_points_become_ahead_and_left_of_the_chair() -> None:
    # Facing east: a point further east is ahead, a point further north is to the left.
    assert world_to_chair(np.array([5.0, 0.0]), 0, 0, 90.0)[0] == pytest.approx([5.0, 0.0])
    assert world_to_chair(np.array([0.0, 5.0]), 0, 0, 90.0)[0] == pytest.approx([0.0, 5.0])


def test_footprint_takes_best_clear_path() -> None:
    ahead, to_the_left = straight(score=1.0), straight(left=1.6, score=0.8)

    assert choose([ahead, to_the_left], []).index == 0

    person = Obstacle("p", "pedestrian", 2.5, 0.0)
    detour = choose([ahead, to_the_left], [person])
    assert (detour.index, detour.status) == (1, "clear")
    assert detour.reason == "Going around obstacle"


def test_footprint_ignores_what_is_far_then_slows_then_stops() -> None:
    def wall(at: float) -> list[Obstacle]:
        return [Obstacle(f"p{i}", "pedestrian", at, y) for i, y in enumerate((-0.8, 0.0, 0.8))]

    far = choose([straight(reach=8.0)], wall(7.0))
    assert (far.status, far.speed_factor) == ("clear", 1.0)

    closing = choose([straight()], wall(4.0))
    assert closing.status == "slowing"
    assert 0.0 < closing.speed_factor < 1.0

    stopped = choose([straight()], wall(2.0))
    assert (stopped.status, stopped.speed_factor) == ("blocked", 0.0)
    assert stopped.reason == "Waiting for pedestrian"


def test_footprint_slows_when_passing_close() -> None:
    # Outside the margin, inside the caution band.
    beside = Obstacle("cone", "object", 2.0, 1.0, radius_m=0.2)
    passing = choose([straight()], [beside])
    assert (passing.status, passing.reason) == ("slowing", "Slowing for obstacle")


def test_the_models_own_speed_reaches_the_command_at_once() -> None:
    # No ramp: the speed the model asked for is the speed commanded, from a standstill too.
    assert follow(straight().path_xy, 1.0, speed_limit_mps=0.3) == pytest.approx((0.3, 0.0))
    # Slowing for an obstacle scales the model's speed; blocked stops, with no turn either.
    assert follow(straight().path_xy, 0.5, speed_limit_mps=0.3)[0] == pytest.approx(0.15)
    assert follow(straight(left=1.0).path_xy, 0.0, speed_limit_mps=0.3) == (0.0, 0.0)


def test_steering_is_proportional_outside_a_small_dead_zone() -> None:
    def turn_toward(off_deg: float) -> float:
        off = math.radians(off_deg)
        return follow(np.array([[2.0 * math.cos(off), 2.0 * math.sin(off)]]), 1.0)[1]

    # A target under a degree off is driven straight at: noise is not steered for.
    assert turn_toward(0.5) == 0.0 and turn_toward(-0.5) == 0.0
    # Further off, the turn is toward the target (positive left) and grows with the angle.
    assert 0.0 < turn_toward(10.0) < turn_toward(25.0)
    assert turn_toward(-10.0) == pytest.approx(-turn_toward(10.0))
    # A path beside the chair is faced before it is driven.
    assert follow(np.array([[0.0, 2.0]]), 1.0)[0] == 0.0
