"""Drive the chair from the live phone feed: frames in, stick out (Phase 1, runs on the board).

Reads the driving service's model feed, asks the driver for paths back to back,
steers toward a point a little way along the chosen path, and sends stick
percent to the board daemon. A stale frame or a failed call sends nothing, so the
sender's hold runs out and the chair stops.

    python -m fsd_ml.drive_loop --driver remote:https://<omnivla>.modal.run --dry
    WHEELCHAIR_HOST=127.0.0.1 python -m fsd_ml.drive_loop --driver remote:<url> --log drive.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import time

import httpx
import numpy as np

from fsd_ml.chair_out import MAX_PCT
from fsd_ml.drivers import Goal, Motion, make_driver
from fsd_ml.live import LiveFeed
from fsd_ml.pipeline.footprint import choose

MAX_FRAME_AGE_MS = 500.0
# A path that never gets further than this from the chair means "stay put".
STOP_REACH_M = 0.15


def path_to_stick(
    path_xy: np.ndarray, fwd: float, k: float, lookahead_m: float
) -> tuple[int, int, float, float, float]:
    """(x, y, ahead, left, angle_deg) to steer at the lookahead point. x>0 right, y>0 forward."""
    path = np.asarray(path_xy, dtype=float).reshape(-1, 2)
    distances = np.linalg.norm(path, axis=1)
    if len(path) == 0 or distances.max() < STOP_REACH_M:
        return 0, 0, 0.0, 0.0, 0.0
    beyond = np.nonzero(distances >= lookahead_m)[0]
    ahead, left = path[beyond[0]] if len(beyond) else path[-1]
    angle = math.atan2(left, ahead)
    x = round(-max(-MAX_PCT, min(MAX_PCT, k * angle)))
    # Behind or beside the chair: turn on the spot rather than drive away from the path.
    y = round(fwd * max(0.4, math.cos(angle))) if ahead > 0 else 0
    return x, y, float(ahead), float(left), math.degrees(angle)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--driver", required=True, help="e.g. remote:https://...modal.run")
    parser.add_argument("--service", default="http://127.0.0.1:8000")
    parser.add_argument("--goal", default="5,0", help="ahead,left in metres")
    parser.add_argument("--dry", action="store_true", help="decide and log, never move the chair")
    parser.add_argument("--fwd", type=float, default=25.0, help="forward stick %% on a straight")
    parser.add_argument("--k", type=float, default=60.0, help="turn stick %% per radian")
    parser.add_argument("--lookahead", type=float, default=1.0, help="metres along the path")
    parser.add_argument("--log", help="append one JSON line per decision here")
    args = parser.parse_args(argv)
    goal = Goal(point_xy=tuple(float(v) for v in args.goal.split(",")))

    driver = make_driver(args.driver)
    feed = LiveFeed(args.service)
    feed.start()
    sender = None
    if not args.dry:
        from fsd_ml.chair_out import ChairSender

        sender = ChairSender()
    log = open(args.log, "a") if args.log else None  # noqa: SIM115
    last = time.monotonic()
    try:
        while True:
            frame = feed.latest().frame
            now = time.monotonic()
            age_ms = (now - frame.captured_s) * 1000 if frame and frame.captured_s else math.inf
            if age_ms > MAX_FRAME_AGE_MS:
                time.sleep(0.05)  # nothing sent: the hold runs out and the chair stops
                continue
            frames = feed.window(driver.context_frames, driver.frame_hz)
            try:
                candidates = driver.propose(frames, goal, Motion())
            except httpx.HTTPError as error:
                print(f"driver call failed: {error}")
                continue
            choice = choose(candidates, [])
            steer = path_to_stick(choice.path_xy, args.fwd, args.k, args.lookahead)
            x, y, ahead, left, angle = steer
            now = time.monotonic()
            entry = {
                "t": time.time(),
                "frame_age_ms": round(age_ms, 1),
                "round_trip_ms": getattr(driver, "round_trip_ms", None),
                "inference_ms": getattr(driver, "inference_ms", None),
                "ahead": round(ahead, 2),
                "left": round(left, 2),
                "angle_deg": round(angle, 1),
                "x": x,
                "y": y,
                "decision_hz": round(1 / max(now - last, 1e-3), 2),
            }
            last = now
            if sender is not None:
                result = sender.send_stick(x, y)
                if result.halted:
                    print(f"stopping: {result.problem}; the rider has control")
                    break
            print(json.dumps(entry))
            if log:
                log.write(json.dumps(entry) + "\n")
                log.flush()
    except KeyboardInterrupt:
        pass
    finally:
        if sender is not None:
            sender.stop()
            sender.close()
        feed.stop()
        if log:
            log.close()


if __name__ == "__main__":
    main()
