"""The iPad's trip as a goal for the model: the signs and the frames of reference."""

from __future__ import annotations

import json
import math

import pytest

from fsd_ml import live
from fsd_ml.geo import EARTH_RADIUS_M


def to_latlon(east: float, north: float, origin: tuple[float, float]) -> tuple[float, float]:
    lat = origin[0] + math.degrees(north / EARTH_RADIUS_M)
    lon = origin[1] + math.degrees(east / (EARTH_RADIUS_M * math.cos(math.radians(origin[0]))))
    return lat, lon


def test_ipad_trip_becomes_a_goal_for_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = (38.99, -76.94)
    chair = to_latlon(0.0, 5.0, origin)  # 5 m up a route that runs north, then east
    here = {"lat": chair[0], "lon": chair[1], "fresh": True}
    north = {"deg": 0.0, "reference": "true"}
    reader = live.ChairGoal("https://chair.example")

    assert reader.read({})[0] is None
    said = {"goal": {"kind": "instruction", "version": 1, "instruction": "go to the kitchen"}}
    goal, note = reader.read(said)
    assert (goal.instruction, goal.point_xy) == ("go to the kitchen", None)
    assert "go to the kitchen" in note

    # A bare destination 20 m due east of a chair facing north is 20 m to its right.
    bare = {"destination": {"distance_m": 20.0, "bearing_deg": 90.0}, "gps": here, "heading": north}
    goal, note = reader.read(bare)
    assert goal.point_xy == pytest.approx((0.0, -20.0), abs=1e-6)
    assert "no route" in note
    # Close enough is there: no goal, so the chair is stopped.
    near = {**bare, "destination": {"distance_m": 2.0, "bearing_deg": 90.0}}
    assert reader.read(near) == (None, live.ARRIVED)
    # Without knowing which way the chair faces there is nothing to aim at.
    assert reader.read({**bare, "heading": {"deg": 0.0, "reference": "magnetic"}})[0] is None

    points = [to_latlon(e, n, origin) for e, n in ((0, 0), (0, 12), (30, 12))]
    fetched = []

    class Answer:
        def raise_for_status(self):
            return self

        def json(self):
            return {"data": {"route": [{"lat": lat, "lon": lon} for lat, lon in points]}}

    def get(url: str, timeout: float) -> Answer:
        fetched.append(url)
        return Answer()

    monkeypatch.setattr(live.httpx, "get", get)
    routed = {
        "goal": {"kind": "route", "version": 2, "instruction": None, "route_points": 3},
        "destination": {"distance_m": 30.0, "bearing_deg": 80.0},
        "gps": here,
        "heading": north,
    }
    goal, note = reader.read(routed)
    # 10 m further along: 7 m north to the corner, then 3 m east, so ahead and to the right.
    assert goal.point_xy == pytest.approx((7.0, -3.0), abs=0.05)
    assert note == "iPad route: 37 m left"
    reader.read(routed)
    assert fetched == ["https://chair.example/api/goal"]  # the route is fetched once per version


def test_the_chairs_angle_is_the_services_own_sum_however_its_messages_arrive() -> None:
    now = [0.0]
    feed = live.LiveFeed("http://chair.example", clock=lambda: now[0])

    def hear(at_s: float, yaw_deg: float) -> None:
        now[0] = at_s
        motion = {"fresh": True, "yaw_rate_dps": 20.0, "yaw_deg": yaw_deg}
        feed._take(json.dumps({"motion": motion}))

    # Unevenly spaced messages: what counts is how far the service says it has turned.
    for at_s, yaw in [(0.0, 100.0), (0.02, 101.0), (0.3, 106.0), (0.31, 110.0)]:
        hear(at_s, yaw)
    assert math.degrees(feed.yaw_rad()) == pytest.approx(10.0)
    # After a silence the service's sum may have started again: no jump, count on from it.
    hear(5.0, 0.0)
    hear(5.1, 2.0)
    assert math.degrees(feed.yaw_rad()) == pytest.approx(12.0)
