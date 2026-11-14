from __future__ import annotations

import numpy as np
import pytest

from fsd_ml.drivers import Candidate, FanDriver, Goal, Motion
from fsd_ml.geo import chair_to_world, to_latlon, to_local, world_to_chair
from fsd_ml.pipeline import ChairStatus, Obstacle, Pipeline, Route, Sensors
from fsd_ml.pipeline.control import follow
from fsd_ml.pipeline.footprint import choose
from fsd_ml.pipeline.outputs import command_message, drive_state

ORIGIN = (38.9897, -76.9378)
FRAME = np.zeros((4, 4, 3), dtype=np.uint8)


def route_from(points: list[tuple[float, float]]) -> Route:
    return Route([to_latlon(east, north, ORIGIN) for east, north in points])


def straight(reach: float = 6.0, left: float = 0.0, score: float = 1.0) -> Candidate:
    x = np.linspace(0.25, reach, 24)
    return Candidate(np.column_stack([x, np.full_like(x, left)]), score)


def sensors(t_ms: float, east: float = 0.0, north: float = 0.0, **changes) -> Sensors:
    lat, lon = to_latlon(east, north, ORIGIN)
    values = dict(
        frame=FRAME,
        frame_t_ms=t_ms,
        lat=lat,
        lon=lon,
        fix_t_ms=t_ms,
        heading_deg=0.0,
        speed_mps=0.0,
        yaw_rate_radps=0.0,
    )
    return Sensors(t_ms, **{**values, **changes})


def test_frames_convert_both_ways() -> None:
    east, north = to_local(*to_latlon(12.0, -7.0, ORIGIN), ORIGIN)
    assert (east, north) == pytest.approx((12.0, -7.0), abs=1e-6)

    # Facing east: a point further east is ahead, a point further north is to the left.
    assert world_to_chair(np.array([5.0, 0.0]), 0, 0, 90.0)[0] == pytest.approx([5.0, 0.0])
    assert world_to_chair(np.array([0.0, 5.0]), 0, 0, 90.0)[0] == pytest.approx([0.0, 5.0])
    back = chair_to_world(world_to_chair(np.array([[3.0, 4.0]]), 1, 2, 37.0), 1, 2, 37.0)
    assert back[0] == pytest.approx([3.0, 4.0])


def test_route_progress_and_points() -> None:
    route = route_from([(0, 0), (0, 30), (30, 30)])

    assert route.length_m == pytest.approx(60.0, abs=0.01)
    assert route.project(1.0, 10.0) == pytest.approx(10.0, abs=0.01)
    assert route.point_at(45.0) == pytest.approx([15.0, 30.0], abs=0.01)
    assert route.ahead(28.0, 4.0)[-1] == pytest.approx([2.0, 30.0], abs=0.01)
    # A fix near the far leg cannot drag progress there from the start.
    assert route.project(29.0, 29.0, near_m=2.0) <= 32.0


def test_route_from_ipad_shape() -> None:
    lat, lon = to_latlon(0.0, 20.0, ORIGIN)
    route = Route.from_app(
        {"points": [{"lat": ORIGIN[0], "lng": ORIGIN[1]}, {"lat": lat, "lng": lon}]}
    )
    assert route.length_m == pytest.approx(20.0, abs=0.01)
    with pytest.raises(ValueError):
        Route([ORIGIN, ORIGIN])


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
    assert stopped.yielding_to_id is not None


def test_footprint_slows_when_passing_close() -> None:
    # Outside the margin, inside the caution band.
    beside = Obstacle("cone", "object", 2.0, 1.0, radius_m=0.2)
    passing = choose([straight()], [beside])
    assert (passing.status, passing.reason) == ("slowing", "Slowing for obstacle")


def test_control_follows_the_path() -> None:
    speed, turn = follow(straight().path_xy, 1.0, current_speed_mps=1.0, dt_s=0.05)
    assert speed == pytest.approx(1.0)
    assert turn == pytest.approx(0.0)

    x = np.linspace(0.2, 4.0, 20)
    _, left_turn = follow(np.column_stack([x, 0.15 * x * x]), 1.0, 0.5, 0.05)
    assert left_turn > 0.0  # positive turns left

    # From rest the chair accelerates, it does not jump to cruise.
    assert follow(straight().path_xy, 1.0, 0.0, 0.05)[0] == pytest.approx(0.04)
    assert follow(straight().path_xy, 0.0, 1.0, 0.05) == (0.0, 0.0)
    # A path beside the chair is faced before it is driven.
    assert follow(np.array([[0.0, 2.0]]), 1.0, 0.0, 0.05)[0] == 0.0


def test_fan_driver_offers_detours_that_end_at_the_goal() -> None:
    candidates = FanDriver().propose([FRAME], Goal(point_xy=(5.0, 0.0)), Motion())

    assert len(candidates) == 5
    assert candidates[0].score == max(c.score for c in candidates)
    for candidate in candidates:
        assert candidate.path_xy[-1] == pytest.approx([5.0, 0.0], abs=1e-6)
    assert max(abs(c.path_xy[:, 1]).max() for c in candidates) == pytest.approx(1.4, abs=0.01)


def test_pipeline_holds_until_engaged_then_drives() -> None:
    pipeline = Pipeline(FanDriver())
    chair = ChairStatus()

    idle = pipeline.step(sensors(1000.0), chair, 0.05)
    assert (idle.mode, idle.engage_blocked) == ("idle", "Set a destination first")
    assert idle.command.v_mps == 0.0

    pipeline.set_route(route_from([(0, 0), (0, 30)]))
    ready = pipeline.step(sensors(1050.0), chair, 0.05)
    assert (ready.mode, ready.engage_blocked, ready.command.v_mps) == ("ready", None, 0.0)
    assert len(ready.path_xy) > 0  # the path is shown before the chair moves

    pipeline.engage()
    driving = pipeline.step(sensors(1100.0), chair, 0.05)
    assert driving.mode == "driving"
    assert driving.command.v_mps > 0.0
    assert driving.command.seq == ready.command.seq + 1


def test_pipeline_stops_on_stale_camera_and_on_joystick() -> None:
    pipeline = Pipeline(FanDriver())
    pipeline.set_route(route_from([(0, 0), (0, 30)]))
    pipeline.step(sensors(1000.0), ChairStatus(), 0.05)
    pipeline.engage()

    stale = pipeline.step(sensors(1800.0, frame=None, frame_t_ms=None), ChairStatus(), 0.05)
    assert (stale.command.v_mps, stale.reason) == (0.0, "Camera feed lost")
    assert stale.take_over is None  # not yet: a short gap only stops the chair
    later = pipeline.step(sensors(4000.0, frame=None, frame_t_ms=None), ChairStatus(), 0.05)
    assert later.take_over == "Camera feed lost"

    held = pipeline.step(sensors(4050.0), ChairStatus(mode="manual", joystick_active=True), 0.05)
    assert (held.mode, held.command.v_mps) == ("manual", 0.0)
    released = pipeline.step(sensors(4100.0), ChairStatus(), 0.05)
    assert released.mode == "ready"  # does not resume by itself


def test_outputs_match_the_documented_shapes() -> None:
    pipeline = Pipeline(FanDriver())
    pipeline.set_route(route_from([(0, 0), (0, 30)]))
    person = Obstacle("p", "pedestrian", 5.0, 1.0)
    decision = pipeline.step(sensors(1000.0, obstacles=(person,)), ChairStatus(), 0.05)

    assert set(command_message(decision)) == {"type", "seq", "t", "v_mps", "w_radps", "ttl_ms"}
    state = drive_state(decision, pipeline.origin, ChairStatus(battery_pct=80))
    assert state["position"]["lat"] == pytest.approx(ORIGIN[0])
    # Ahead and left become the iPad's z (ahead) and x (right).
    assert state["obstacles"] == [{"id": "p", "kind": "pedestrian", "x": -1.0, "z": 5.0}]
    assert state["path"][-1]["z"] > 0.0
    assert state["batteryPct"] == 80


def test_model_buffer_is_thinned_to_the_models_frame_rate() -> None:
    class Slow(FanDriver):
        context_frames = 3
        frame_hz = 5.0

    pipeline = Pipeline(Slow())
    pipeline.set_route(route_from([(0, 0), (0, 30)]))

    # 20 fps in, 5 fps kept: three kept frames need 400 ms, not 100.
    waiting = [pipeline.step(sensors(1000.0 + 50 * i), ChairStatus(), 0.05) for i in range(8)]
    assert all(d.gate_reason == "Waiting for driver model" for d in waiting)
    ready = pipeline.step(sensors(1400.0), ChairStatus(), 0.05)
    assert ready.gate_reason is None
