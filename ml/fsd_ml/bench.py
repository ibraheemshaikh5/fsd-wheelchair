"""The test bench: a fixed set of short runs, the feedback on each, and the notes back.

The loop it is for:

1. the person at the viewer runs a test and watches the chair;
2. they say how it went (a rating, a measurement, a few words);
3. that lands in the journal, where it is read before the next change is made;
4. the notes say what was changed, what that is expected to fix, and which test to run next.

Everything the runs depend on is in ``settings.json`` and is read afresh for every run, so a
change takes effect on the next press of Run, with no restart.

Files, all under ``runs/viewer/bench``::

    settings.json   how the chair is driven (stick strengths, ramps, calibration)
    journal.jsonl   every run and every piece of feedback, in order
    notes.json      what was changed and why, and what to try next; shown on the page
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# How the chair is driven in a test. Stick numbers are percent before the board adds the
# joystick's dead zone, so 1 is the gentlest thing the chair will respond to at all.
DEFAULTS: dict[str, float] = {
    # Forward stick on a straight, and how long it takes to ease up to it and back down.
    "drive_stick_pct": 6.0,
    "ramp_s": 0.8,
    # Stick while turning on the spot, and how early it is released (the chair coasts on).
    "turn_stick_pct": 1.0,
    "turn_lead_deg": 10.0,
    "turn_done_deg": 5.0,
    "turn_passes": 3.0,
    # Standing still between one move and the next.
    "settle_s": 1.0,
    # A reverse pull to stop harder. Off: it is the harshest thing the chair can be asked for.
    "brake_pct": 0.0,
    "brake_ms": 0.0,
    # What full stick is believed to give. Measured runs correct these.
    "v_full_mps": 1.5,
    "w_full_radps": 1.5,
    # Floor left between the chair's front and a target, and space kept beside set moves.
    "arrive_gap_m": 0.6,
    "clearance_m": 0.10,
    # Top stick percent when steering to a target.
    "goto_speed_pct": 30.0,
    # How many times the model pilot may look and decide before the run is called off.
    "pilot_max_looks": 30.0,
}

FEET = 0.3048

# The tests. ``ask`` is what to measure or notice; it labels the box on the feedback form.
CASES: list[dict[str, Any]] = [
    {
        "id": "pilot",
        "title": "Claude drives to a named thing",
        "does": "Claude Sonnet sees the camera, picks each move itself (forward, back, turn), "
        "and says when it has arrived. The chair still stops itself for obstacles.",
        "ask": "Did it choose sensible moves? Where did it go wrong, if it did?",
        "kind": "pilot",
        "params": {"say": "go to the red pillow"},
        "input": "say",
    },
    {
        "id": "straight",
        "title": "Straight line, 2½ ft",
        "does": "Eases forward in a straight line for 2½ ft, then eases to a stop.",
        "ask": "How far did it really go (inches)? Did it stay straight?",
        "kind": "straight",
        "params": {"distance_m": 2.5 * FEET},
    },
    {
        "id": "turn_left",
        "title": "Quarter turn left",
        "does": "Turns 90° to the left on the spot, checking itself with the gyro.",
        "ask": "Was it really a quarter turn? Smooth or jerky?",
        "kind": "turn",
        "params": {"degrees_left": 90.0},
    },
    {
        "id": "turn_right",
        "title": "Quarter turn right",
        "does": "Turns 90° to the right on the spot.",
        "ask": "Was it really a quarter turn? Same feel as the left turn?",
        "kind": "turn",
        "params": {"degrees_left": -90.0},
    },
    {
        "id": "square",
        "title": "Square, 2½ ft sides",
        "does": "Four straight sides with a quarter turn left after each. Ends where it began.",
        "ask": "How far from the start did it end (inches)? Which corner was worst?",
        "kind": "square",
        "params": {"side_m": 2.5 * FEET, "left": True},
    },
    {
        "id": "circle",
        "title": "Circle, 3 ft wide",
        "does": "Drives one full circle to the left.",
        "ask": "How wide was the circle (inches)? Did it close?",
        "kind": "circle",
        "params": {"diameter_m": 3 * FEET, "left": True},
    },
    {
        "id": "goto",
        "title": "Go to a named thing",
        "does": "Finds what you name, steers to it round anything in the way, stops 2 ft short.",
        "ask": "Did it pick the right thing and stop in the right place?",
        "kind": "go",
        "params": {"say": "go to the red pillow"},
        "input": "say",
    },
]


def case(case_id: str) -> dict[str, Any] | None:
    return next((c for c in CASES if c["id"] == case_id), None)


class Bench:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._settings = folder / "settings.json"
        self._journal = folder / "journal.jsonl"
        self._notes = folder / "notes.json"
        self._count = 0

    def settings(self) -> dict[str, float]:
        """The defaults, with whatever ``settings.json`` changes. Read afresh every time."""
        merged = dict(DEFAULTS)
        try:
            saved = json.loads(self._settings.read_text())
            merged.update({k: float(v) for k, v in saved.items() if k in DEFAULTS})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return merged

    def set(self, **changes: float) -> dict[str, float]:
        """Change settings and remember them. Unknown names are refused."""
        unknown = set(changes) - set(DEFAULTS)
        if unknown:
            raise KeyError(f"no such setting: {', '.join(sorted(unknown))}")
        try:
            saved = json.loads(self._settings.read_text())
        except (OSError, ValueError):
            saved = {}
        saved.update({name: float(value) for name, value in changes.items()})
        self.folder.mkdir(parents=True, exist_ok=True)
        self._settings.write_text(json.dumps(saved, indent=2) + "\n")
        return self.settings()

    def notes(self) -> dict[str, Any]:
        try:
            return json.loads(self._notes.read_text())
        except (OSError, ValueError):
            return {"summary": None, "next": None, "cases": {}, "changes": []}

    def write(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Add a line to the journal: a run starting, its result, or feedback on it."""
        entry = {"t": round(time.time(), 2), "kind": kind, **fields}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self._journal.open("a") as lines:
                lines.write(json.dumps(entry) + "\n")
        except (OSError, TypeError, ValueError):
            pass
        return entry

    def new_run(self, case_id: str, params: dict[str, Any]) -> str:
        self._count += 1
        run_id = f"{time.strftime('%m%d-%H%M%S')}-{case_id}"
        self.write("run", run=run_id, case=case_id, params=params, settings=self.settings())
        return run_id

    def journal(self, last: int = 40) -> list[dict[str, Any]]:
        try:
            lines = self._journal.read_text().splitlines()[-last:]
        except OSError:
            return []
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def latest(self) -> dict[str, dict[str, Any]]:
        """For each test, its newest run with the result and the feedback given, if any."""
        runs: dict[str, dict[str, Any]] = {}
        newest: dict[str, str] = {}
        for entry in self.journal(400):
            run = entry.get("run")
            if entry["kind"] == "run":
                runs[run] = {"run": run, "case": entry["case"], "t": entry["t"]}
                newest[entry["case"]] = run
            elif run in runs:
                runs[run][entry["kind"]] = {
                    k: v for k, v in entry.items() if k not in ("kind", "run")
                }
        return {case_id: runs[run] for case_id, run in newest.items()}
