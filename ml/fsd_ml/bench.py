"""The test bench: a few short set runs, and the settings every way of driving reads.

Everything about how the chair is driven is in ``runs/console/bench/settings.json`` and is
read afresh for every run and every command, so a change takes effect at once, with no
restart. Change them on the bench page, or edit the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# How the chair is driven. Stick numbers are percent before the board adds the joystick's
# dead zone, so 1 is the gentlest thing the chair will respond to at all.
DEFAULTS: dict[str, float] = {
    # Forward stick on a straight, and how long it takes to ease up to it and back down.
    "drive_stick_pct": 6.0,
    "ramp_s": 0.8,
    # Stick while turning on the spot, and how early it is released (the chair coasts on).
    "turn_stick_pct": 12.0,
    "turn_lead_deg": 10.0,
    "turn_done_deg": 5.0,
    "turn_passes": 3.0,
    # Standing still between one move and the next.
    "settle_s": 1.0,
    # A reverse pull to stop harder. Off: it is the harshest thing the chair can be asked for.
    "brake_pct": 0.0,
    "brake_ms": 0.0,
    # Measured on the chair: the 6% drive setting goes ~0.45 m/s and 10% ~0.75 m/s (taped
    # start line, 4 s and 1.8 s runs). Only valid near those settings: speed isn't linear in stick.
    "v_full_mps": 7.5,
    "w_full_radps": 1.5,
    # Space kept around the chair (front and sides) on a set or pilot move. Depth reads
    # 10-25% long up close, so this is generous.
    "clearance_m": 0.25,
    # How much of the driving model's stick is sent: 100 is all of it.
    "model_speed_pct": 40.0,
    # How many times the model pilot may look and decide before the run is called off.
    "pilot_max_looks": 30.0,
}

FEET = 0.3048

CASES: list[dict[str, Any]] = [
    {
        "id": "pilot",
        "title": "Claude drives to a named thing",
        "does": "Claude sees the camera, picks each move itself (forward, back, turn), "
        "and says when it has arrived. The chair still stops itself for obstacles.",
        "kind": "pilot",
        "params": {"say": "go to the red pillow"},
        "input": "say",
    },
    {
        "id": "gemini",
        "title": "Gemini drives to a named thing",
        "does": "Paths the chair fits along are drawn and numbered on the camera's picture; "
        "Gemini picks one (or a turn) each look, and says when it has arrived. The chair "
        "still stops itself for obstacles.",
        "kind": "gemini",
        "params": {"say": "go to the red pillow"},
        "input": "say",
    },
    {
        "id": "straight",
        "title": "Straight line, 2½ ft",
        "does": "Eases forward in a straight line for 2½ ft, then eases to a stop.",
        "kind": "straight",
        "params": {"distance_m": 2.5 * FEET},
    },
    {
        "id": "turn_left",
        "title": "Quarter turn left",
        "does": "Turns 90° to the left on the spot, checking itself with the gyro.",
        "kind": "turn",
        "params": {"degrees_left": 90.0},
    },
    {
        "id": "turn_right",
        "title": "Quarter turn right",
        "does": "Turns 90° to the right on the spot.",
        "kind": "turn",
        "params": {"degrees_left": -90.0},
    },
]


def case(case_id: str) -> dict[str, Any] | None:
    return next((c for c in CASES if c["id"] == case_id), None)


class Bench:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._settings = folder / "settings.json"

    def _saved(self) -> dict[str, float]:
        try:
            saved = json.loads(self._settings.read_text())
            return {k: float(v) for k, v in saved.items() if k in DEFAULTS}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def settings(self) -> dict[str, float]:
        """The defaults, with whatever ``settings.json`` changes. Read afresh every time."""
        return {**DEFAULTS, **self._saved()}

    def set(self, **changes: float) -> dict[str, float]:
        """Change settings and remember them. Unknown names are refused."""
        unknown = set(changes) - set(DEFAULTS)
        if unknown:
            raise KeyError(f"no such setting: {', '.join(sorted(unknown))}")
        saved = {**self._saved(), **{name: float(value) for name, value in changes.items()}}
        self.folder.mkdir(parents=True, exist_ok=True)
        self._settings.write_text(json.dumps(saved, indent=2) + "\n")
        return self.settings()
