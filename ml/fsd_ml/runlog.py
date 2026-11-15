"""Sum up a live run from the viewer's record, for reading after a test.

    uv run python -m fsd_ml.runlog                 # the newest run
    uv run python -m fsd_ml.runlog runs/viewer/live/20261114-193000

A run folder holds ``log.jsonl`` (one line per decision, stop and setting) and the
camera frame each decision was made on, named by the frame's number.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

RUNS = Path("runs/viewer/live")


def read(run: Path) -> list[dict[str, Any]]:
    lines = (run / "log.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _spread(values: list[float]) -> str:
    if not values:
        return "none"
    ordered = sorted(values)
    high = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return f"median {statistics.median(ordered):.0f}, 95th {high:.0f}"


def summary(events: list[dict[str, Any]]) -> list[str]:
    decisions = [e for e in events if e["event"] == "decision"]
    out = []
    if not decisions:
        return ["no decisions recorded"]
    seconds = decisions[-1]["t"] - decisions[0]["t"]
    rate = (len(decisions) - 1) / seconds if seconds > 0 else 0.0
    out.append(f"{len(decisions)} decisions over {seconds:.0f} s ({rate:.1f} per second)")
    out.append(f"model: {decisions[-1].get('model')}")
    for name, label in (
        ("inference_ms", "model time ms"),
        ("round_trip_ms", "round trip ms"),
        ("frame_age_ms", "frame age ms"),
    ):
        out.append(f"{label}: {_spread([d[name] for d in decisions if d.get(name) is not None])}")

    goals = Counter(d.get("say") or d.get("ipad") or d.get("goal_kind") for d in decisions)
    out.append("goals: " + "; ".join(f"{goal} x{count}" for goal, count in goals.most_common(4)))

    looked = [d["seen"] for d in decisions if (d.get("seen") or {}).get("target")]
    if looked:
        found = [s for s in looked if s["boxes"]]
        far = [s["boxes"][0]["distance_m"] for s in found if s["boxes"][0].get("distance_m")]
        line = f"detector: target in view in {len(found)} of {len(looked)} looks"
        if far:
            line += f", distance {min(far):.1f} to {max(far):.1f} m (last {far[-1]:.1f})"
        out.append(line)

    turn = [d["w_radps"] for d in decisions if d.get("w_radps") is not None]
    if turn:
        left = sum(1 for w in turn if w > 0.05)
        right = sum(1 for w in turn if w < -0.05)
        out.append(
            f"steering: left {left}, right {right}, straight {len(turn) - left - right};"
            f" mean turn {statistics.mean(turn):+.2f} rad/s"
        )
    reach = [max((p[0] for p in d["path"]), default=0.0) for d in decisions if d.get("path")]
    if reach:
        out.append(f"path reach ahead cm: {_spread([r * 100 for r in reach])}")

    sent = [d["chair"] for d in decisions if (d.get("chair") or {}).get("sent")]
    if sent:
        ys, xs = [c["y"] for c in sent], [c["x"] for c in sent]
        out.append(
            f"sent to chair: {len(sent)} commands, forward stick {min(ys)} to {max(ys)}%,"
            f" turn stick {min(xs)} to {max(xs)}%"
        )
    else:
        out.append("sent to chair: nothing")
    for event in events:
        if event["event"] in ("sending on", "settings", "stop", "waiting"):
            detail = {k: v for k, v in event.items() if k not in ("t", "event", "board")}
            out.append(f"  +{event['t'] - events[0]['t']:6.1f} s  {event['event']}  {detail}")
    problems = Counter(
        d["chair"]["problem"] for d in decisions if (d.get("chair") or {}).get("problem")
    )
    for problem, count in problems.items():
        out.append(f"not sent x{count}: {problem}")
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run", nargs="?", type=Path, help="a run folder; default is the newest")
    args = parser.parse_args(argv)
    run = args.run or max(RUNS.iterdir(), default=None)
    if run is None:
        raise SystemExit(f"no runs in {RUNS}")
    print(run)
    print("\n".join(summary(read(run))))


if __name__ == "__main__":
    main()
