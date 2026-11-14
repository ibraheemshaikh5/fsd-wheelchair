"""Drive the pipeline through a scenario with a simulated chair and phone.

    uv run python -m fsd_ml.sim.run            # every scenario
    uv run python -m fsd_ml.sim.run crossing   # one

Each run writes, under ``runs/sim/<scenario>/``: ``decisions.jsonl`` (the
decision record), ``commands.jsonl`` and ``drive_state.jsonl`` (what would go to
the chair and the iPad) and ``track.png`` (a top-down picture of the drive).

The simulated camera is blank, so this exercises the pipeline, not the model.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from fsd_ml.drivers import make_driver
from fsd_ml.geo import to_latlon, world_to_chair
from fsd_ml.pipeline import ModelRunner, Obstacle, Pipeline, PipelineConfig, Route, Sensors
from fsd_ml.pipeline.outputs import command_message, drive_state, record
from fsd_ml.pipeline.types import Decision
from fsd_ml.sim.chair import SimChair
from fsd_ml.sim.scenarios import SCENARIOS, Scenario, SimObstacle

ORIGIN = (38.9897, -76.9378)
START_MS = 1_794_650_000_000.0
DT_S = 0.05
BLANK_FRAME = np.zeros((36, 64, 3), dtype=np.uint8)
# How far the simulated obstacle stage can see.
SENSE_RANGE_M = 12.0


@dataclass(slots=True)
class SimResult:
    scenario: str
    arrived: bool
    duration_s: float
    collided: bool
    # Closest any obstacle came to the chair's body; None with no obstacles.
    min_clearance_m: float | None
    # (seconds, what changed) each time the chair's visible state changes.
    events: list[tuple[float, str]] = field(default_factory=list)
    out_dir: Path | None = None


def _in(window: tuple[float, float] | None, t_s: float) -> bool:
    return window is not None and window[0] <= t_s < window[1]


def _position(obstacle: SimObstacle, t_s: float) -> tuple[float, float]:
    moved = t_s - obstacle.from_s
    return obstacle.east + obstacle.velocity[0] * moved, obstacle.north + obstacle.velocity[
        1
    ] * moved


def _seen(scenario: Scenario, chair: SimChair, t_s: float) -> tuple[Obstacle, ...]:
    """What a perfect obstacle stage would report, in the chair's frame."""
    seen = []
    for obstacle in scenario.obstacles:
        if not obstacle.from_s <= t_s < obstacle.until_s:
            continue
        ahead, left = world_to_chair(
            np.array(_position(obstacle, t_s)), chair.east, chair.north, chair.heading_deg
        )[0]
        if ahead > -1.5 and math.hypot(ahead, left) <= SENSE_RANGE_M:
            seen.append(
                Obstacle(obstacle.id, obstacle.kind, float(ahead), float(left), obstacle.radius_m)
            )
    return tuple(seen)


def _clearance(obstacles: tuple[Obstacle, ...], config: PipelineConfig) -> float | None:
    """Gap between the chair's body (no margin) and the nearest obstacle; negative is a hit."""
    shape = config.footprint.shape
    gaps = [
        math.hypot(max(abs(o.x) - shape.length_m / 2, 0.0), max(abs(o.y) - shape.width_m / 2, 0.0))
        - o.radius_m
        for o in obstacles
    ]
    return min(gaps) if gaps else None


def _summary(decision: Decision) -> str:
    parts = [decision.mode]
    if decision.mode == "driving":
        parts.append(decision.path_status)
    if decision.reason:
        parts.append(decision.reason)
    if decision.take_over:
        parts.append(f"TAKE OVER: {decision.take_over}")
    return " | ".join(parts)


def simulate(
    scenario: Scenario,
    driver_name: str = "fan",
    out_dir: Path | None = None,
    config: PipelineConfig | None = None,
    real_timing: bool = False,
) -> SimResult:
    config = config or PipelineConfig()
    route = Route([to_latlon(east, north, ORIGIN) for east, north in scenario.route])
    (e0, n0), (e1, n1) = scenario.route[0], scenario.route[1]
    chair = SimChair(e0, n0, heading_deg=math.degrees(math.atan2(e1 - e0, n1 - n0)) % 360.0)

    def latency(now_ms: float) -> float:
        dies = scenario.model_dies_at_s
        if dies is not None and (now_ms - START_MS) / 1000.0 >= dies:
            return math.inf
        return scenario.model_latency_ms

    driver = make_driver(driver_name)
    pipeline = Pipeline(
        driver, config, ModelRunner(driver, scenario.model_period_ms, latency, real_timing)
    )
    pipeline.set_route(route)

    result = SimResult(scenario.name, False, 0.0, False, None)
    track: list[tuple[float, float, str]] = []
    files = {}
    if out_dir is not None:
        result.out_dir = out_dir / scenario.name
        result.out_dir.mkdir(parents=True, exist_ok=True)
        files = {
            name: (result.out_dir / f"{name}.jsonl").open("w")
            for name in ("decisions", "commands", "drive_state")
        }

    last_fix: tuple[float, float, float, float] | None = None
    last_summary = None
    t_s = 0.0
    try:
        while t_s < scenario.max_duration_s:
            now_ms = START_MS + t_s * 1000.0
            chair.joystick_active = _in(scenario.joystick_s, t_s)
            chair.estop = scenario.estop_at_s is not None and t_s >= scenario.estop_at_s

            obstacles = _seen(scenario, chair, t_s)
            if _in(scenario.phone_dropout_s, t_s):
                # Nothing new arrives; the last fix just gets older.
                lat, lon, heading, fix_t = last_fix or (None, None, None, None)
                sensors = Sensors(now_ms, lat=lat, lon=lon, heading_deg=heading, fix_t_ms=fix_t)
            else:
                lat, lon = to_latlon(chair.east, chair.north, ORIGIN)
                last_fix = (lat, lon, chair.heading_deg, now_ms)
                sensors = Sensors(
                    now_ms,
                    BLANK_FRAME,
                    now_ms,
                    lat,
                    lon,
                    now_ms,
                    chair.heading_deg,
                    chair.speed_mps,
                    chair.yaw_rate_radps,
                    obstacles,
                )

            status = chair.status()
            decision = pipeline.step(sensors, status, DT_S)
            if decision.mode == "ready" and decision.engage_blocked is None:
                # Stands in for the rider pressing Start.
                pipeline.engage()

            chair.receive(decision.command)
            chair.step(now_ms, DT_S)

            summary = _summary(decision)
            if summary != last_summary:
                result.events.append((round(t_s, 2), summary))
                last_summary = summary
            gap = _clearance(obstacles, config)
            if gap is not None:
                result.min_clearance_m = (
                    gap if result.min_clearance_m is None else min(result.min_clearance_m, gap)
                )
                result.collided = result.collided or gap < 0.0
            track.append(
                (
                    chair.east,
                    chair.north,
                    decision.path_status if chair.speed_mps > 0.02 else "stopped",
                )
            )
            if files:
                files["decisions"].write(json.dumps(record(decision, status)) + "\n")
                files["commands"].write(json.dumps(command_message(decision)) + "\n")
                files["drive_state"].write(
                    json.dumps(drive_state(decision, pipeline.origin, status)) + "\n"
                )

            t_s += DT_S
            if decision.mode == "arrived" and chair.speed_mps < 0.01:
                result.arrived = True
                break
    finally:
        for file in files.values():
            file.close()

    result.duration_s = round(t_s, 2)
    if result.out_dir is not None:
        _draw(scenario, track, result.out_dir / "track.png")
    return result


_COLOURS = {
    "clear": (40, 160, 80),
    "slowing": (230, 160, 30),
    "blocked": (210, 60, 50),
    "stopped": (210, 60, 50),
}


def _draw(scenario: Scenario, track: list[tuple[float, float, str]], path: Path) -> None:
    """A top-down picture: route in grey, the chair's track coloured by what it was doing."""
    points = np.array([*scenario.route, *[(e, n) for e, n, _ in track]])
    low, high = points.min(axis=0) - 4.0, points.max(axis=0) + 4.0
    scale = 14.0
    size = ((high - low) * scale).astype(int)

    def px(east: float, north: float) -> tuple[float, float]:
        return (east - low[0]) * scale, (high[1] - north) * scale

    image = Image.new("RGB", (int(size[0]), int(size[1])), "white")
    draw = ImageDraw.Draw(image)
    draw.line([px(*p) for p in scenario.route], fill=(190, 190, 190), width=int(1.5 * scale))
    for obstacle in scenario.obstacles:
        x, y = px(obstacle.east, obstacle.north)
        r = obstacle.radius_m * scale
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(60, 60, 60))
    for east, north, state in track[::2]:
        x, y = px(east, north)
        draw.ellipse([x - 2.5, y - 2.5, x + 2.5, y + 2.5], fill=_COLOURS[state])
    image.save(path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("scenario", nargs="?", default="all", choices=["all", *SCENARIOS])
    parser.add_argument("--driver", default="fan")
    parser.add_argument("--out", type=Path, default=Path("runs/sim"))
    parser.add_argument(
        "--real-timing",
        action="store_true",
        help="make each model result as late as the call really took",
    )
    args = parser.parse_args(argv)

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    for name in names:
        scenario = SCENARIOS[name]
        result = simulate(scenario, args.driver, args.out, real_timing=args.real_timing)
        outcome = "arrived" if result.arrived else "did not arrive"
        expected = "" if result.arrived == scenario.expect_arrival else "  <-- UNEXPECTED"
        clearance = (
            "" if result.min_clearance_m is None else f", closest {result.min_clearance_m:.2f} m"
        )
        hit = ", COLLISION" if result.collided else ""
        print(f"\n{name}: {scenario.description}")
        print(f"  {outcome} in {result.duration_s:.1f} s{clearance}{hit}{expected}")
        for t_s, event in result.events:
            print(f"  {t_s:6.2f}s  {event}")
        print(f"  -> {result.out_dir}")


if __name__ == "__main__":
    main()
