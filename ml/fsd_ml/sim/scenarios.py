"""Situations to drive the pipeline through. Positions are metres east, north of the start."""

from __future__ import annotations

import math
from dataclasses import dataclass

from fsd_ml.pipeline.types import ObstacleKind

# North 30 m, then east 30 m.
L_ROUTE = ((0.0, 0.0), (0.0, 30.0), (30.0, 30.0))


@dataclass(frozen=True, slots=True)
class SimObstacle:
    id: str
    kind: ObstacleKind
    east: float
    north: float
    radius_m: float = 0.3
    velocity: tuple[float, float] = (0.0, 0.0)
    from_s: float = 0.0
    until_s: float = math.inf


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    route: tuple[tuple[float, float], ...] = L_ROUTE
    obstacles: tuple[SimObstacle, ...] = ()
    # (start, end) seconds during which the phone sends nothing.
    phone_dropout_s: tuple[float, float] | None = None
    # (start, end) seconds during which the rider holds the joystick.
    joystick_s: tuple[float, float] | None = None
    estop_at_s: float | None = None
    model_period_ms: float = 100.0
    model_latency_ms: float = 0.0
    # The model stops answering from this time on.
    model_dies_at_s: float | None = None
    max_duration_s: float = 120.0
    expect_arrival: bool = True


def _crowd(until_s: float = math.inf) -> tuple[SimObstacle, ...]:
    """People standing across the whole path."""
    return tuple(
        SimObstacle(f"person-{i}", "pedestrian", east, 15.0, until_s=until_s)
        for i, east in enumerate((-1.8, -0.9, 0.0, 0.9, 1.8))
    )


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario("clear", "Nothing in the way."),
        Scenario(
            "pedestrian",
            "One person standing on the path; the chair goes around.",
            obstacles=(SimObstacle("person-0", "pedestrian", 0.1, 12.0),),
        ),
        Scenario(
            "crossing",
            "A group blocks the whole path, then moves on; the chair waits and continues.",
            obstacles=_crowd(until_s=20.0),
        ),
        Scenario(
            "stuck",
            "A group blocks the whole path and stays; the chair asks the rider to take over.",
            obstacles=_crowd(),
            max_duration_s=40.0,
            expect_arrival=False,
        ),
        Scenario(
            "dropout",
            "The phone stops streaming for 4 s; the chair stops, then carries on.",
            phone_dropout_s=(8.0, 12.0),
        ),
        Scenario(
            "joystick",
            "The rider grabs the joystick for 4 s, then re-engages.",
            joystick_s=(8.0, 12.0),
        ),
        Scenario(
            "estop",
            "The e-stop is pressed mid-trip.",
            estop_at_s=10.0,
            max_duration_s=20.0,
            expect_arrival=False,
        ),
        Scenario(
            "slow-model",
            "The model answers every 500 ms, 400 ms late.",
            model_period_ms=500.0,
            model_latency_ms=400.0,
        ),
        Scenario(
            "dead-model",
            "The model stops answering mid-trip.",
            model_dies_at_s=10.0,
            max_duration_s=25.0,
            expect_arrival=False,
        ),
    )
}
