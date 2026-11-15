"""The record of a run on the chair: what was sent, on which camera frame, and what came back.

A run is one connection of the console to the chair's driving service. Its folder,
``runs/console/live/<date-time>/``, holds ``log.jsonl`` and ``frames/``. Every way of driving
(keyboard, bench moves, the driving model, the pilot) writes one ``command`` line per stick
command or decision::

    {"t": 1794650000.123,            # when the line was written, unix seconds
     "event": "command",
     "mode": "manual" | "bench" | "model" | "pilot",
     "frame": "frames/0001234.jpg",  # the camera's own JPEG the command was given on
     "frame_n": 1234,                # the feed's count of that frame
     "frame_t": 1794650000.051,      # when the phone took it, unix seconds
     "stick": [x, y],                # percent sent, x right, y forward; null if none was
     "sent": true, "problem": null,  # whether it reached the chair, and why not
     "model": {...},                 # what the model said: its path, or the pilot's tool call
     "board": {...}}                 # the board's telemetry at that moment

Other lines (``connected``, ``sending on``, ``stop``, ``waiting``, ``run finished`` ...) mark
what happened around them.

    uv run python -m fsd_ml.runlog            # sum up the newest run
    uv run python -m fsd_ml.runlog runs/console/live/20261114-193000
"""

from __future__ import annotations

import argparse
import json
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
            line = json.dumps({"t": round(time.time(), 3), "event": event, **fields})
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
