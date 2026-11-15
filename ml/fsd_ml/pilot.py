"""A frontier model drives the chair: it is shown the camera's picture and answers with moves.

The model (Claude Sonnet) is given the task in words, a picture, and a short report of what
the chair's own obstacle sense finds nearby. It answers by calling tools: go forward, go
back, turn left, turn right, or say it is done. Several can be called at once and are run in
order. After they run it gets the results and a new picture, and so on.

It decides where to go. It is never trusted to stop the chair: every move it asks for is
carried out by the same runner as the other tests, which stops for obstacles, for STOP, and
for the joystick, whatever the model wanted.

Its instructions are in ``ml/pilot_prompt.md`` and are read afresh for every look, so they
can be changed between runs without restarting anything. The API key comes from the
environment (``ANTHROPIC_API_KEY``; ``uv run --env-file .env`` sets it from ml/.env).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODEL = "claude-sonnet-5-5"
PROMPT_FILE = Path(__file__).resolve().parent.parent / "pilot_prompt.md"
# What an earlier picture is replaced with once a newer one has been sent.
OLD_PICTURE = {"type": "text", "text": "[An earlier picture from the camera, no longer shown.]"}
# The most any one call may ask for. Longer moves are cut to these, and the model is told.
MAX_FORWARD_M = 1.0
MAX_BACKWARD_M = 0.4
MAX_TURN_DEG = 120.0

_WHY = {"type": "string", "description": "One plain sentence: what you see and what this is for."}


def _move(name: str, description: str, amount: str, unit: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {amount: {"type": "number", "description": unit}, "why": _WHY},
            "required": [amount, "why"],
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    _move(
        "move_forward",
        f"Drive straight ahead. At most {MAX_FORWARD_M} m in one call.",
        "distance_m",
        "Metres to go forward.",
    ),
    _move(
        "move_backward",
        f"Drive straight back, where the camera cannot see. At most {MAX_BACKWARD_M} m.",
        "distance_m",
        "Metres to go back.",
    ),
    _move("turn_left", "Turn on the spot to the left.", "degrees", "Degrees to turn."),
    _move("turn_right", "Turn on the spot to the right.", "degrees", "Degrees to turn."),
    {
        "name": "done",
        "description": "End the task: you have arrived, or you cannot do it.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "arrived": {"type": "boolean", "description": "True if the goal was reached."},
                "why": _WHY,
            },
            "required": ["arrived", "why"],
            "additionalProperties": False,
        },
    },
]


@dataclass(frozen=True, slots=True)
class Call:
    id: str
    name: str
    input: dict[str, Any]

    @property
    def why(self) -> str:
        return str(self.input.get("why", ""))

    def says(self) -> str:
        """The call in words, for the page and the record."""
        if self.name in ("move_forward", "move_backward"):
            way = "forward" if self.name == "move_forward" else "back"
            return f"{way} {float(self.input.get('distance_m', 0)):.2f} m"
        if self.name in ("turn_left", "turn_right"):
            way = "left" if self.name == "turn_left" else "right"
            return f"turn {way} {float(self.input.get('degrees', 0)):.0f}°"
        return "arrived" if self.input.get("arrived") else "giving up"


@dataclass(slots=True)
class Pilot:
    """One task's conversation with the model."""

    task: str
    model: str = MODEL
    effort: str = "low"
    client: Any = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    # How the last reply went, for the record.
    last: dict[str, Any] = field(default_factory=dict)

    def _prompt(self) -> str:
        return PROMPT_FILE.read_text()

    def next(
        self, jpeg: bytes, report: str, results: list[tuple[str, str, bool]] | None = None
    ) -> list[Call]:
        """Show the model the newest picture and what its last moves did; get its next moves.

        ``results`` is (tool call id, what happened, whether it failed) for every call in
        its previous reply. Returns no calls if the model would not or could not answer.

        Only the newest picture is sent: older ones are replaced with a line of text, since
        the chair has moved on from them and they are most of what a request costs.
        """
        if self.client is None:
            import anthropic

            self.client = anthropic.Anthropic(timeout=40.0, max_retries=1)
        for message in self.messages:
            if message["role"] == "user":
                message["content"] = [
                    OLD_PICTURE if block["type"] == "image" else block
                    for block in message["content"]
                ]
        content: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": failed}
            for call_id, text, failed in results or []
        ]
        picture = base64.standard_b64encode(jpeg).decode()
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": picture},
            }
        )
        opening = f"Your task: {self.task}\n\n" if not self.messages else ""
        content.append({"type": "text", "text": f"{opening}{report}"})
        self.messages.append({"role": "user", "content": content})
        started = time.perf_counter()
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=self._prompt(),
            tools=TOOLS,
            # A quick decision about once every few seconds, not a long think.
            thinking={"type": "between_tools"},
            output_config={"effort": self.effort},
            messages=self.messages,
        )
        # The reply goes back next turn without its thinking blocks: those are only valid
        # while everything before them is unchanged, and the picture before them is swapped
        # for a line of text on the next look.
        kept = [b for b in response.content if b.type not in ("thinking", "redacted_thinking")]
        self.messages.append({"role": "assistant", "content": kept})
        usage = response.usage
        self.last = {
            "ms": round((time.perf_counter() - started) * 1000.0),
            "stop_reason": response.stop_reason,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "text": " ".join(b.text for b in response.content if b.type == "text").strip(),
        }
        if response.stop_reason != "tool_use":
            return []
        return [Call(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
