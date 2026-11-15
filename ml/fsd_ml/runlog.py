"""The record of a run on the chair: what was sent, on which camera frame, and what came back.

A run is one connection of the console to the chair's driving service. Its folder,
``runs/console/live/<date-time>/``, holds ``log.jsonl`` and ``frames/``. Every way of driving
(keyboard, bench moves, the driving model, the pilots) writes one ``command`` line per stick
command or decision::

    {"t": 1794650000.123,            # when the line was written, unix seconds
     "event": "command",
     "mode": "manual" | "bench" | "model" | "pilot" | "gemini",
     "frame": "frames/0001234.jpg",  # the camera's own JPEG the command was given on
     "frame_n": 1234,                # the feed's count of that frame
     "frame_t": 1794650000.051,      # when the phone took it, unix seconds
     "stick": [x, y],                # percent sent, x right, y forward; null if none was
     "sent": true, "problem": null,  # whether it reached the chair, and why not
     "model": {...},                 # what the model said: its path, or the pilot's tool call
     "asked": [x, y],                # the stick asked for, before it was rounded and capped
     "yaw_deg": 12.3,                # the gyro's summed yaw (positive left) and its freshness
     "gyro_fresh": true,
     "pose": {"x_m": 1.2, "y_m": 0.1},  # believed: metres from where the connection began,
                                     # along the gyro's zero heading and to its left, from
                                     # the calibrated speed of the stick sent and the yaw
     "frame_seen_n": 1236,           # the count and age of the frame the command was given
     "frame_age_s": 0.06,            # on (``frame`` is the newest one saved, at most 10 a
     "camera_fresh": true,           # second), and whether that is under half a second
     "obstacle_age_s": 0.12,         # how old the obstacle sense's picture was
     "action": 3,                    # the move of a run it belongs to (see episodes below)
     "picture": "frames/0001234.picks.jpg",  # gemini only: the frame as the model was shown it
     "board": {...}}                 # the board's telemetry at that moment

A ``gemini`` decision's ``model`` holds what a training sample needs: ``say`` (the task),
``paths`` (every path offered, by number, as [ahead, left] metres for the chair's middle, a
point every 0.1 m), ``choice``, ``scene``, ``note``, ``ms`` and the token counts.

Other lines (``connected``, ``sending on``, ``stop``, ``waiting``, ``run finished`` ...) mark
what happened around them. Every line also has ``mono``, this machine's monotonic clock.

Each bench or pilot run is also an episode, ``episode-NNN.jsonl`` in the same folder: the
whole run in one file, in order, with nothing left out. Every line has ``t`` (unix
seconds), ``mono`` (monotonic seconds) and ``type``:

- ``episode_start``: the task, the kind of run, the code's git commit, the camera and chair
  calibration (ml/chair.json), every bench setting, and the model (name, thinking, its
  prompt file and that file's sha256).
- ``tick``: one per stick command (about 20 a second while a move is under way), with
  everything a ``command`` line has: the stick asked for and sent, the frame it was given
  on, the gyro's summed ``yaw_deg``, the believed ``pose``, the age of the obstacle sense's
  picture, and whether gyro and camera are fresh.
- ``model_call``: one per look: the picture exactly as the model was sent it (``picture``),
  the raw frame, the full prompt text, what was on offer, the model's raw output and what
  was made of it, how long it took, and the tokens.
- ``action_start`` / ``action_end``: every turn, straight, back and path: what was planned
  (degrees, metres, the path's points) and what was achieved (the gyro's degrees, believed
  metres), how long it took, and ``ended``: finished, time_cap, obstacle, no_floor, stuck,
  stale_gyro, no_speed, takeover, link, heartbeat, disarmed, stopped or exception, with the
  words the model was told in ``detail``.
- ``warning`` (a command given on a camera frame older than half a second), ``exception``
  (with its traceback), ``refused`` (a move the model asked for that was not run).
- ``episode_end``: the outcome and reason, ``success`` (null: for a person to fill in),
  totals (seconds, looks, actions, stops by reason, takeovers, warnings, ticks), and
  ``coverage``: of the stick commands, the share given on a fresh camera frame and with a
  fresh gyro, how many different frames they were given on, and the oldest frame any was
  given on. ``usable`` is false when under USABLE_SHARE of them had a fresh frame and gyro:
  a recording made while the phone had stopped streaming is driving with no pictures.

Driving by hand from the keyboard is an episode too (``kind: manual``), from switching it on
to switching it off, so a demonstration is checked the same way.

That is what a dataset export needs (LeRobot: frames, timestamps, the stick as the action,
the believed pose as the state, the task; GNM: frames and poses): no exporter is written yet.

    uv run python -m fsd_ml.runlog            # sum up the newest run
    uv run python -m fsd_ml.runlog runs/console/live/20261114-193000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from fsd_ml.live import LiveFrame

RUNS = Path("runs/console/live")


class RunLog:
    """Writes one run's ``log.jsonl`` and frames. Never gets in the way of driving."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        (folder / "frames").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # The last frame saved, as a command line names it, and when it was saved.
        self._last: dict[str, Any] = {"frame": None, "frame_n": None, "frame_t": None}
        self._last_s = 0.0

    def write(self, event: str, **fields: Any) -> None:
        try:
            stamp = {"t": round(time.time(), 3), "mono": round(time.monotonic(), 3)}
            line = json.dumps({**stamp, "event": event, **fields})
            with self._lock, (self.folder / "log.jsonl").open("a") as lines:
                lines.write(line + "\n")
        except (OSError, TypeError, ValueError):
            pass

    def frame(self, frame: LiveFrame | None, min_gap_s: float = 0.0) -> dict[str, Any]:
        """Save a frame's JPEG and return the fields that name it in a command line.

        With ``min_gap_s``, a frame saved less than that long ago stands in for this one, so
        commands sent many times a second do not each write a picture.
        """
        now = time.monotonic()
        with self._lock:
            if frame is None or not frame.jpeg:
                return dict.fromkeys(self._last)
            if frame.n == self._last["frame_n"] or now - self._last_s < min_gap_s:
                return dict(self._last)
            name = f"frames/{frame.n:07d}.jpg"
            try:
                (self.folder / name).write_bytes(frame.jpeg)
            except OSError:
                return dict.fromkeys(self._last)
            self._last = {
                "frame": name,
                "frame_n": frame.n,
                "frame_t": round(time.time() - frame.age_s(now), 3),
            }
            self._last_s = now
            return dict(self._last)

    def episode(self, **header: Any) -> Episode:
        """Begin the record of one run: the next ``episode-NNN.jsonl`` in this folder."""
        number = len(list(self.folder.glob("episode-*.jsonl"))) + 1
        return Episode(self.folder / f"episode-{number:03d}.jsonl", number, header)


def code_version() -> dict[str, Any]:
    """The git commit this code is at, and whether files differ from it."""
    here = Path(__file__).resolve().parent

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", *args], cwd=here, capture_output=True, text=True, timeout=5, check=True
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip()

    changed = git("status", "--porcelain", "--untracked-files=no")
    return {"commit": git("rev-parse", "HEAD"), "dirty": None if changed is None else bool(changed)}


def prompt_stamp(prompt: Path) -> dict[str, Any]:
    """A prompt file and the sha256 of what it holds now."""
    try:
        return {"file": prompt.name, "sha256": hashlib.sha256(prompt.read_bytes()).hexdigest()}
    except OSError:
        return {"file": prompt.name, "sha256": None}


# An episode is usable as data only if this share of its commands had a fresh camera frame
# and a fresh gyro.
USABLE_SHARE = 0.95


class Episode:
    """One run's whole record, in order, in its own file. See the top of this module."""

    def __init__(self, path: Path, number: int, header: dict[str, Any]) -> None:
        self.path, self.number = path, number
        self._lock = threading.Lock()
        self._began = time.monotonic()
        self._counts: Counter[str] = Counter()
        self._stops: Counter[str] = Counter()
        # Of its stick commands: how many had a fresh camera frame, a fresh gyro, which
        # frames they were given on, and the oldest frame any was given on.
        self._fresh: Counter[str] = Counter()
        self._frames: set[Any] = set()
        self._oldest_s = 0.0
        self.write("episode_start", episode=number, code=code_version(), **header)

    def write(self, kind: str, /, **fields: Any) -> None:
        self._counts[kind] += 1
        if kind == "action_end" and fields.get("ended") != "finished":
            self._stops[str(fields.get("ended"))] += 1
        if kind == "tick":
            self._fresh["camera"] += bool(fields.get("camera_fresh"))
            self._fresh["gyro"] += bool(fields.get("gyro_fresh"))
            self._frames.add(fields.get("frame_seen_n"))
            age = fields.get("frame_age_s")
            self._oldest_s = max(self._oldest_s, math.inf if age is None else age)
        try:
            stamp = {"t": round(time.time(), 3), "mono": round(time.monotonic(), 3)}
            line = json.dumps({**stamp, "type": kind, **fields}, default=str)
            with self._lock, self.path.open("a") as lines:
                lines.write(line + "\n")
        except (OSError, TypeError, ValueError):
            pass

    def end(self, outcome: Any, reason: Any) -> None:
        ticks = self._counts["tick"]
        camera, gyro = (self._fresh[name] / ticks if ticks else 1.0 for name in ("camera", "gyro"))
        self.write(
            "episode_end",
            outcome=outcome,
            reason=reason,
            # For a person to fill in afterwards: did it really do the task?
            success=None,
            totals={
                "seconds": round(time.monotonic() - self._began, 1),
                "looks": self._counts["model_call"],
                "actions": self._counts["action_end"],
                "stops": dict(self._stops),
                "takeovers": self._stops["takeover"],
                "warnings": self._counts["warning"],
                "ticks": ticks,
            },
            coverage={
                "camera_fresh": round(camera, 3),
                "gyro_fresh": round(gyro, 3),
                "frames": len(self._frames - {None}),
                "oldest_frame_s": None if math.isinf(self._oldest_s) else round(self._oldest_s, 2),
            },
            usable=min(camera, gyro) >= USABLE_SHARE,
        )


def read(run: Path) -> list[dict[str, Any]]:
    lines = (run / "log.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def summary(events: list[dict[str, Any]]) -> list[str]:
    commands = [e for e in events if e["event"] == "command"]
    if not commands:
        return ["no commands recorded"]
    out = [f"{len(commands)} commands over {commands[-1]['t'] - commands[0]['t']:.0f} s"]
    for mode, count in Counter(c["mode"] for c in commands).items():
        sticks = [c["stick"] for c in commands if c["mode"] == mode and c.get("stick")]
        line = f"{mode}: {count} commands"
        if sticks:
            xs, ys = [s[0] for s in sticks], [s[1] for s in sticks]
            line += f", stick x {min(xs)} to {max(xs)}%, y {min(ys)} to {max(ys)}%"
        out.append(line)
    out.append(f"with a frame: {sum(1 for c in commands if c.get('frame'))} of {len(commands)}")
    for problem, count in Counter(c["problem"] for c in commands if c.get("problem")).items():
        out.append(f"not sent x{count}: {problem}")
    for event in events:
        if event["event"] != "command":
            detail = {k: v for k, v in event.items() if k not in ("t", "event", "board")}
            out.append(f"  +{event['t'] - events[0]['t']:6.1f} s  {event['event']}  {detail}")
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
