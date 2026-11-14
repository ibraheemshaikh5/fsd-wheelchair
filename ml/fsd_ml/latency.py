"""Measure the round trip to a served driver: the number that decides where models run.

uv run python -m fsd_ml.latency http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse

import numpy as np

from fsd_ml.drivers import Goal, Motion
from fsd_ml.remote import RemoteDriver


def measure(url: str, calls: int = 30, width: int = 384, height: int = 216) -> dict[str, float]:
    driver = RemoteDriver(url)
    rng = np.random.default_rng(0)
    frames = [
        rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
        for _ in range(driver.context_frames)
    ]
    goal, motion = Goal(point_xy=(5.0, 0.0)), Motion(1.0, 0.0)
    driver.propose(frames, goal, motion)  # the first call pays for the connection

    round_trips, inferences = [], []
    for _ in range(calls):
        driver.propose(frames, goal, motion)
        round_trips.append(driver.round_trip_ms)
        inferences.append(driver.inference_ms)
    driver.close()
    return {
        "round_trip_p50_ms": float(np.percentile(round_trips, 50)),
        "round_trip_p95_ms": float(np.percentile(round_trips, 95)),
        "inference_p50_ms": float(np.percentile(inferences, 50)),
        "network_p50_ms": float(np.percentile(np.subtract(round_trips, inferences), 50)),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url")
    parser.add_argument("--calls", type=int, default=30)
    args = parser.parse_args(argv)
    for name, value in measure(args.url, args.calls).items():
        print(f"{name:20} {value:8.1f}")


if __name__ == "__main__":
    main()
