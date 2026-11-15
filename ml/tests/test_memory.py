"""Obstacles are remembered after they leave the view, and forgotten only for a reason."""

import math

from fsd_ml.memory import FORGET_AFTER_S, ObstacleMemory, View
from fsd_ml.pipeline.footprint import ChairShape, FootprintConfig, first_hit
from fsd_ml.pipeline.types import Obstacle

DT = 0.1
VIEW = View(lens_xy=(0.0, 0.0), hfov_deg=80.0, near_m=1.0, far_m=3.1)


class World:
    """Fixed things in a room, and a chair that really moves through it."""

    def __init__(self, things: list[tuple[float, float]]) -> None:
        self.things = list(things)
        self.x = self.y = self.heading = 0.0
        self.now = 0.0

    def drive(self, speed: float, turn: float = 0.0) -> None:
        self.heading += turn * DT / 2
        self.x += speed * DT * math.cos(self.heading)
        self.y += speed * DT * math.sin(self.heading)
        self.heading += turn * DT / 2
        self.now += DT

    def from_chair(self, wx: float, wy: float) -> tuple[float, float]:
        dx, dy = wx - self.x, wy - self.y
        cos, sin = math.cos(-self.heading), math.sin(-self.heading)
        return dx * cos - dy * sin, dx * sin + dy * cos

    def seen(self) -> list[Obstacle]:
        """What a camera looking ahead sees: 40 degrees each side, 0.4 to 3.5 m."""
        out = []
        for index, (wx, wy) in enumerate(self.things):
            x, y = self.from_chair(wx, wy)
            if 0.4 < math.hypot(x, y) < 3.5 and abs(math.degrees(math.atan2(y, x))) < 40:
                out.append(Obstacle(f"o{index}", "object", x, y, 0.09))
        return out


def wall(x: float, y0: float, y1: float) -> list[tuple[float, float]]:
    return [(x, y0 + 0.15 * i) for i in range(int((y1 - y0) / 0.15) + 1)]


def run(world, memory, seconds, speed=0.0, turn=0.0, told_speed=None, look=True):
    for _ in range(round(seconds / DT)):
        world.drive(speed, turn)
        memory.move(speed if told_speed is None else told_speed, turn, DT)
        if look:
            memory.see(world.seen(), world.now)


def remembered(memory) -> list[tuple[float, float]]:
    return [(o.x, o.y) for o in memory.obstacles() if o.id.startswith("m")]


def test_a_thing_passed_on_the_left_stays_on_the_map_beside_the_chair():
    world = World([(2.0, 0.9)])
    memory = ObstacleMemory(VIEW)
    run(world, memory, 0.5)
    assert len(memory.obstacles()) == 1
    run(world, memory, 4.0, speed=0.5)  # drive 2 m: the thing is now level with the chair
    assert world.seen() == []
    ((x, y),) = remembered(memory)
    true_x, true_y = world.from_chair(2.0, 0.9)
    assert abs(x - true_x) < 0.05 and abs(y - true_y) < 0.05
    run(world, memory, 4.0, speed=0.5)  # and now well behind: it no longer matters
    assert memory.obstacles() == []


def test_turning_on_the_spot_swings_the_map_the_other_way():
    world = World([(2.0, 0.0)])
    memory = ObstacleMemory(VIEW)
    run(world, memory, 0.5)
    run(world, memory, math.pi / 2 / 0.8, turn=0.8)  # a quarter turn to the left
    ((x, y),) = remembered(memory)  # the thing that was ahead is now on the right
    assert abs(x) < 0.15 and abs(y + 2.0) < 0.15


def test_the_map_turns_about_the_drive_wheels_not_the_chairs_middle():
    memory = ObstacleMemory(VIEW, pivot_x=-0.35)
    memory.see([Obstacle("o", "object", 2.0, 0.0, 0.09)], 0.0)
    memory.see([Obstacle("o", "object", 2.0, 0.0, 0.09)], 0.1)
    memory.move(0.0, math.pi / 2, 1.0)
    thing = memory.things[0]
    assert abs(thing.x + 0.35) < 1e-6 and abs(thing.y + 2.35) < 1e-6


def test_a_thing_that_leaves_while_in_clear_view_is_cleared():
    world = World([(2.0, 0.0)])
    memory = ObstacleMemory(VIEW)
    run(world, memory, 0.5)
    world.things.clear()  # the person walks off
    run(world, memory, 0.3)
    assert len(memory.obstacles()) == 1  # not at once: one missed look is not proof
    run(world, memory, 0.6)
    assert memory.obstacles() == []


def test_a_thing_hidden_behind_a_nearer_one_is_not_cleared():
    world = World([(2.5, 0.0)])
    memory = ObstacleMemory(VIEW)
    run(world, memory, 0.5)
    hidden = Obstacle("near", "object", 1.4, 0.05, 0.09)
    for step in range(30):  # three seconds of only the nearer thing being seen
        memory.see([hidden], world.now + step * DT)
    assert any(abs(x - 2.5) < 0.05 for x, _ in remembered(memory))


def test_things_too_near_or_at_the_edge_of_view_are_not_cleared_by_not_seeing_them():
    memory = ObstacleMemory(VIEW)
    for now in (0.0, 0.1):
        memory.see(
            [Obstacle("a", "object", 0.6, 0.0, 0.09), Obstacle("b", "object", 2.0, 1.6, 0.09)],
            now,
        )
    for step in range(30):
        memory.see([], 0.2 + step * DT)
    assert len(remembered(memory)) == 2


def test_a_single_speck_is_used_while_seen_and_not_kept_after():
    memory = ObstacleMemory(VIEW)
    memory.see([Obstacle("o", "object", 0.7, 0.0, 0.09)], 0.0)
    assert len(memory.obstacles()) == 1
    memory.see([], 0.1)
    assert memory.obstacles() == []
    memory.see([], 0.5)
    assert memory.things == []


def test_a_flickering_thing_is_still_confirmed():
    memory = ObstacleMemory(VIEW)
    thing = [Obstacle("o", "object", 0.7, 0.0, 0.09)]
    memory.see(thing, 0.0)
    memory.see([], 0.1)
    memory.see(thing, 0.2)
    memory.see([], 0.3)
    assert len(remembered(memory)) == 1


def test_a_covered_camera_clears_nothing_and_the_map_still_moves():
    world = World([(2.0, 0.9)])
    memory = ObstacleMemory(VIEW)
    run(world, memory, 0.5)
    run(world, memory, 2.0, speed=0.5, look=False)  # no looks at all while it drives a metre
    ((x, _),) = [(o.x, o.y) for o in memory.obstacles()]
    assert abs(x - 1.0) < 0.05


def test_what_is_in_view_corrects_the_map_when_the_speed_was_wrong():
    # The chair is told it is doing 0.5 m/s; it is really doing 0.3 (the stick is unmeasured).
    things = [(2.0, 0.9), *wall(4.5, -2.0, 2.0)]
    world = World(things)
    memory = ObstacleMemory(VIEW)
    run(world, memory, 0.5)
    run(world, memory, 5.0, speed=0.3, told_speed=0.5)
    true_x, true_y = world.from_chair(2.0, 0.9)
    near = min(remembered(memory), key=lambda p: math.hypot(p[0] - true_x, p[1] - true_y))
    assert math.hypot(near[0] - true_x, near[1] - true_y) < 0.25
    # Without the wall to judge by, the same run would have put it 1 m out.


def test_the_chair_is_not_frozen_by_a_memory_inside_its_clearance():
    shape = ChairShape(0.58, 1.02, 0.3)
    memory = ObstacleMemory(VIEW, half_length_m=0.51, half_width_m=0.29, margin_m=0.3)
    beside = Obstacle("o", "object", 0.2, 0.45, 0.09)  # 16 cm off the chair's left side
    memory.see([beside], 0.0)
    memory.see([beside], 0.1)
    memory.see([], 0.2)
    found = memory.obstacles()
    straight = [[0.1 * i, 0.0] for i in range(1, 26)]
    assert first_hit(straight, found, shape) is None  # it may still drive straight on
    left = [
        [math.sin(1.3 * s) / 1.3, (1 - math.cos(1.3 * s)) / 1.3]
        for s in (0.1 * i for i in range(1, 13))
    ]
    assert first_hit(left, found, shape) is not None  # but not turn into it
    config = FootprintConfig(shape=shape)
    assert config.shape.margin_m == 0.3


def test_a_memory_straight_ahead_and_close_still_blocks():
    shape = ChairShape(0.58, 1.02, 0.3)
    memory = ObstacleMemory(VIEW, margin_m=0.3)
    ahead = Obstacle("o", "object", 0.75, 0.0, 0.09)  # too close and low to see any more
    memory.see([ahead], 0.0)
    memory.see([ahead], 0.1)
    memory.see([], 0.2)
    straight = [[0.1 * i, 0.0] for i in range(1, 26)]
    assert first_hit(straight, memory.obstacles(), shape) is not None


def test_a_memory_that_drifts_inside_the_chair_is_dropped():
    memory = ObstacleMemory(VIEW)
    thing = Obstacle("o", "object", 0.9, 0.1, 0.09)
    memory.see([thing], 0.0)
    memory.see([thing], 0.1)
    memory.move(0.8, 0.0, 1.0)  # the map says the chair drove straight through it
    memory.see([], 1.1)
    assert memory.things == []


def test_when_the_view_stops_matching_the_map_the_unseen_is_forgotten():
    world = World([(2.0, 0.9), *wall(3.0, -1.5, 1.5)])
    memory = ObstacleMemory(VIEW)
    run(world, memory, 0.5)
    run(world, memory, 4.0, speed=0.5)
    assert remembered(memory)  # the thing passed on the left is remembered
    # The chair is picked up and put down facing a different, empty stretch of room.
    world.things = [(2.0, 0.9), *wall(2.2, -1.5, 1.5)]
    for step in range(8):
        memory.see(world.seen(), world.now + step * DT)
    assert remembered(memory) == [] and memory.note


def test_nothing_is_remembered_for_ever():
    memory = ObstacleMemory(VIEW)
    beside = Obstacle("o", "object", 0.2, 1.2, 0.09)
    memory.see([beside], 0.0)
    memory.see([beside], 0.1)
    memory.see([], FORGET_AFTER_S - 1.0)
    assert len(remembered(memory)) == 1
    memory.see([], FORGET_AFTER_S + 1.0)
    assert memory.obstacles() == []


def test_a_remembered_thing_grows_with_the_distance_travelled_since_it_was_seen():
    memory = ObstacleMemory(VIEW)
    beside = Obstacle("o", "object", 1.5, 1.6, 0.09)
    memory.see([beside], 0.0)
    memory.see([beside], 0.1)
    memory.see([], 0.2)
    before = memory.obstacles()[0].radius_m
    memory.move(0.5, 0.0, 1.0)
    memory.move(0.5, 0.0, 1.0)
    assert before < memory.obstacles()[0].radius_m <= before + 0.2 + 1e-9


def test_a_long_stall_is_not_treated_as_steady_driving():
    memory = ObstacleMemory(VIEW)
    thing = Obstacle("o", "object", 2.0, 1.6, 0.09)
    memory.see([thing], 0.0)
    memory.see([thing], 0.1)
    memory.move(0.5, 0.0, 30.0)  # the depth model hung for half a minute
    assert memory.things[0].x > 1.4


def test_forgetting_by_hand_empties_the_map():
    memory = ObstacleMemory(VIEW)
    memory.see([Obstacle("o", "object", 2.0, 0.0, 0.09)], 0.0)
    memory.forget("asked")
    assert memory.obstacles() == [] and memory.note == "asked"


def test_a_blind_look_puts_everything_out_of_sight_without_clearing_it():
    memory = ObstacleMemory(VIEW)
    thing = Obstacle("o", "object", 2.0, 0.0, 0.09)  # in clear view, straight ahead
    memory.see([thing], 0.0)
    memory.see([thing], 0.1)
    for _ in range(30):
        memory.blind()
    assert [o.id[0] for o in memory.obstacles()] == ["m"]


def test_a_flicker_far_off_does_not_leave_a_ghost():
    memory = ObstacleMemory(VIEW)
    far = Obstacle("o", "object", 3.3, 0.5, 0.09)  # in the picture, beyond where depth is sure
    memory.see([far], 0.0)
    memory.see([far], 0.1)
    memory.see([], 1.0)
    assert len(remembered(memory)) == 1
    memory.see([], 2.0)
    assert memory.obstacles() == []


def test_a_thing_beside_the_chair_outlasts_a_flicker():
    memory = ObstacleMemory(VIEW)
    beside = Obstacle("o", "object", 0.3, 1.2, 0.09)
    memory.see([beside], 0.0)
    memory.see([beside], 0.1)
    memory.see([], 5.0)
    assert len(remembered(memory)) == 1
