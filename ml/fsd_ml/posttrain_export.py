"""Turn one human-driven stretch into training samples for OmniVLA.

A sample is one camera frame, the task in words, and where the chair was over
the next 8 thirds of a second, seen from where it stood at that frame: the
"waypoints" OmniVLA learns to predict.

What goes in:

* a phone recording made by the drive service (``frames.jsonl``, ``frames/``,
  ``sensors.jsonl``): every frame, and the turn rate from the phone's gyro;
* the console's ``log.jsonl`` for the same minutes: the stick sent to the chair.

Where the chair was is not measured, it is worked out:

* heading: the gyro's turn rate summed up. This is the trustworthy half; bench
  turns come out within about 2 degrees.
* distance: the stick's forward push times the bench speed for full stick
  (``v_full_mps``), started ``RESPONSE_DELAY_S`` late and changing no faster than
  ``ACCEL_MPS2``. Both numbers were read off the phone's forward acceleration
  when the chair set off in the recording of 15 Nov. Nothing measures the
  distance, so expect it to be off by 20-30%, more on short pushes.
* the chair is taken to turn on the spot under the camera. It does not: the
  camera sits ahead and left of the wheels, so a turn also slides the view
  sideways a little. That is left out.

    uv run python -m fsd_ml.posttrain_export \\
        --recording <recordings>/20261115T110020Z \\
        --log runs/console/live/20261115-055304/log.jsonl \\
        --start 1794740467.6 --end 1794740515.4 \\
        --task "go to the bottom of the stairs and stop in front of the first step" \\
        --out runs/posttrain/dataset
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path

import numpy as np

WAYPOINTS = 8
# OmniVLA decides three times a second, and its own data is about that far apart.
WAYPOINT_DT_S = 1 / 3
RESPONSE_DELAY_S = 0.25
ACCEL_MPS2 = 0.9
# Standing frames kept before the first push and after the last, so that
# "not yet" and "stop here" are both in the data without swamping it.
LEAD_IN_S = 1.0
LEAD_OUT_S = 2.0
# The held-out stretch, as fractions of the samples in time order, and how much
# either side of it is thrown away so no training frame is its near-twin.
VAL_SPAN = (0.45, 0.57)
VAL_GUARD_S = 0.5


def heading(t_s: np.ndarray, rate_dps: np.ndarray) -> np.ndarray:
    """Turned so far in radians, positive left, at each gyro sample."""
    steps = np.radians(rate_dps[1:]) * np.diff(t_s)
    return np.concatenate([[0.0], np.cumsum(steps)])


def speed(t_s: np.ndarray, stick_t_s: np.ndarray, asked_mps: np.ndarray) -> np.ndarray:
    """The speed believed at each time: what was asked, late and never jumping."""
    at = np.searchsorted(stick_t_s, t_s - RESPONSE_DELAY_S, side="right") - 1
    asked = np.where(at >= 0, asked_mps[np.maximum(at, 0)], 0.0)
    out = np.zeros_like(t_s)
    for i in range(1, len(t_s)):
        most = ACCEL_MPS2 * (t_s[i] - t_s[i - 1])
        out[i] = out[i - 1] + np.clip(asked[i] - out[i - 1], -most, most)
    return out


def positions(t_s: np.ndarray, yaw_rad: np.ndarray, v_mps: np.ndarray) -> np.ndarray:
    """(x, y) in metres at each time, in the frame the chair started in."""
    step = v_mps[1:] * np.diff(t_s)
    moved = np.column_stack([step * np.cos(yaw_rad[1:]), step * np.sin(yaw_rad[1:])])
    return np.concatenate([[[0.0, 0.0]], np.cumsum(moved, axis=0)])


def waypoints(t_s: np.ndarray, xy: np.ndarray, yaw_rad: np.ndarray, at_s: float) -> np.ndarray:
    """The next 8 poses seen from the pose at ``at_s``: (ahead m, left m, cos, sin)."""
    times = at_s + WAYPOINT_DT_S * np.arange(WAYPOINTS + 1)
    x, y, yaw = (np.interp(times, t_s, column) for column in (xy[:, 0], xy[:, 1], yaw_rad))
    dx, dy, turned = x[1:] - x[0], y[1:] - y[0], yaw[1:] - yaw[0]
    cos, sin = math.cos(yaw[0]), math.sin(yaw[0])
    return np.column_stack(
        [dx * cos + dy * sin, -dx * sin + dy * cos, np.cos(turned), np.sin(turned)]
    )


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def export(
    recording: Path,
    log: Path,
    start_s: float,
    end_s: float,
    task: str,
    out: Path,
    v_full_mps: float,
) -> dict:
    gyro = [
        ((row["t"] + sample[0]) / 1000, sample[2])
        for row in _read(recording / "sensors.jsonl")
        if row["type"] == "motion"
        for sample in row["samples"]
        if sample[2] is not None
    ]
    t_s, rate = (np.array(column) for column in zip(*gyro, strict=True))
    commands = [
        row
        for row in _read(log)
        if row.get("event") == "command" and row["sent"] and start_s <= row["t"] <= end_s
    ]
    stick_t = np.array([row["t"] for row in commands])
    forward_pct = np.array([row["stick"][1] for row in commands], dtype=float)
    pushed = np.array([any(row["stick"]) for row in commands])
    first, last = stick_t[pushed][0], stick_t[pushed][-1]
    begin, finish = first - LEAD_IN_S, last + LEAD_OUT_S

    yaw = heading(t_s, rate)
    v = speed(t_s, stick_t, np.maximum(forward_pct, 0.0) / 100 * v_full_mps)
    xy = positions(t_s, yaw, v)

    need = (t_s >= begin) & (t_s <= finish + WAYPOINTS * WAYPOINT_DT_S)
    worst_gap = float(np.diff(t_s[need]).max())
    if t_s[-1] < finish + WAYPOINTS * WAYPOINT_DT_S or worst_gap > 0.2:
        raise SystemExit(f"the gyro does not cover the stretch (worst gap {worst_gap:.2f} s)")

    frames = [f for f in _read(recording / "frames.jsonl") if begin <= f["t"] / 1000 <= finish]
    lo, hi = (frames[int(len(frames) * part)]["t"] / 1000 for part in VAL_SPAN)
    (out / "frames").mkdir(parents=True, exist_ok=True)
    samples = []
    for frame in frames:
        at = frame["t"] / 1000
        if lo <= at <= hi:
            split = "val"
        elif lo - VAL_GUARD_S <= at <= hi + VAL_GUARD_S:
            continue
        else:
            split = "train"
        name = f"frames/{recording.name}-{Path(frame['file']).name}"
        shutil.copyfile(recording / frame["file"], out / name)
        samples.append(
            {
                "episode": f"{recording.name}@{start_s:.1f}",
                "task": task,
                "split": split,
                "frame": name,
                "t": at,
                "waypoints": np.round(waypoints(t_s, xy, yaw, at), 4).tolist(),
            }
        )
    (out / "samples.jsonl").write_text("".join(json.dumps(s) + "\n" for s in samples))

    inside = (t_s >= begin) & (t_s <= finish)
    turns = np.degrees(np.diff(yaw[inside]))
    ends = np.array([s["waypoints"][-1] for s in samples])
    standing = (np.abs(ends[:, :2]).max(axis=1) < 0.02) & (ends[:, 2] > 0.999)
    stats = {
        "episodes": 1,
        "tasks": {task: len(samples)},
        "samples": {k: sum(s["split"] == k for s in samples) for k in ("train", "val")},
        "seconds": round(finish - begin, 1),
        "frames_per_s": round(len(frames) / (finish - begin), 1),
        "believed_distance_m": round(float(np.sum(v[inside][1:] * np.diff(t_s[inside]))), 2),
        "left_turn_deg": round(float(turns.clip(min=0).sum()), 1),
        "right_turn_deg": round(float(-turns.clip(max=0).sum()), 1),
        "net_turn_deg": round(float(turns.sum()), 1),
        "standing_samples": int(standing.sum()),
        "worst_gyro_gap_s": round(worst_gap, 3),
        "v_full_mps": v_full_mps,
    }
    (out / "stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--start", type=float, required=True, help="wall time, seconds")
    parser.add_argument("--end", type=float, required=True, help="wall time, seconds")
    parser.add_argument("--task", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--v-full-mps", type=float, default=7.5, help="bench speed at full stick")
    args = parser.parse_args()
    stats = export(
        args.recording, args.log, args.start, args.end, args.task, args.out, args.v_full_mps
    )
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
