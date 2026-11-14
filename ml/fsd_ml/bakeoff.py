"""Run a driver over a recorded drive and save what it proposed at each step.

    uv run python -m fsd_ml.bakeoff ../recordings/20261114T150000Z --driver straight

Writes ``<out>/<recording>/<driver>.jsonl``, one line per step. Comparing models,
and drawing the paths on the video, read these files.

The goal is a fixed point straight ahead for now. Recordings carry no route yet,
so this compares how models read the scene, not how they follow a route.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from fsd_ml.drivers import Driver, Goal, Motion, make_driver
from fsd_ml.recording import Recording

DEFAULT_GOAL = Goal(point_xy=(10.0, 0.0))


def run(
    recording: Recording,
    driver: Driver,
    out_dir: Path,
    stride: int = 1,
    goal: Goal = DEFAULT_GOAL,
) -> Path:
    out_path = out_dir / recording.folder.name / f"{driver.name}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as out:
        for window in recording.windows(driver.context_frames, stride):
            latest = window[-1]
            fix = recording.gps_at(latest.t_ms)
            motion = Motion(speed_mps=fix.speed_mps if fix else None)
            frames = [recording.image(frame) for frame in window]

            started = time.perf_counter()
            candidates = driver.propose(frames, goal, motion)
            inference_ms = (time.perf_counter() - started) * 1000.0

            out.write(
                json.dumps(
                    {
                        "n": latest.n,
                        "seq": latest.seq,
                        "t": latest.t_ms,
                        "driver": driver.name,
                        "inference_ms": round(inference_ms, 3),
                        "candidates": [
                            {"score": c.score, "path_xy": c.path_xy.round(3).tolist()}
                            for c in candidates
                        ],
                    }
                )
                + "\n"
            )
    return out_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("recording", type=Path, help="a recording folder")
    parser.add_argument("--driver", default="straight")
    parser.add_argument("--out", type=Path, default=Path("runs"))
    parser.add_argument("--stride", type=int, default=1, help="run every Nth frame")
    args = parser.parse_args(argv)

    out_path = run(Recording(args.recording), make_driver(args.driver), args.out, args.stride)
    print(out_path)


if __name__ == "__main__":
    main()
