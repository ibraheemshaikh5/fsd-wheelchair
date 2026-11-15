"""Gemini drives the chair by picking one of the paths drawn on the camera's picture.

The model (Gemini Robotics-ER by default) is never asked for metres or degrees. Each look it
gets the picture with the paths the chair fits along drawn on the floor and numbered (see
fsd_ml/candidates.py), the task, the list of what it may pick, what its last pick did, how
far the chair has turned since the start, and its own last note. It answers with JSON::

    {"scene": "one sentence", "choice": "3", "note": "one line to its later self"}

``choice`` is a path's number, or L / R (turn 60°), B (turn around), S (arrived), X (give
up). The runner (fsd_ml/moves.py) drives the pick and stops it when it must.

Its instructions are in ``ml/gemini_pilot_prompt.md``, read afresh for every look. The key
comes from the environment (``GEMINI_API_KEY``; ``uv run --env-file .env`` sets it).

To see what it picks on recorded pictures, with nothing connected to the chair::

    uv run --env-file .env --group detect python -m fsd_ml.gemini_pilot \\
        --say "go to the stairs" runs/console/live/<run>/frames/0000123.jpg [more.jpg ...]

That finds obstacles with the depth model when it is installed (else none), draws the
paths, asks once per picture, and saves the drawn pictures under ``runs/gemini/``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODEL = os.environ.get("GEMINI_PILOT_MODEL", "gemini-robotics-er-2-preview")
# low, medium or high: more thinking is slower and may pick better.
THINKING = os.environ.get("GEMINI_PILOT_THINKING", "low")
PROMPT_FILE = Path(__file__).resolve().parent.parent / "gemini_pilot_prompt.md"


@dataclass(slots=True)
class GeminiPilot:
    """One task's conversation with the model."""

    task: str
    model: str = MODEL
    thinking: str = THINKING
    client: Any = None
    prompt_file: Path = PROMPT_FILE
    # Every look and every answer so far, pictures included, so it remembers what it saw.
    contents: list[Any] = field(default_factory=list)
    # How the last reply went, for the record: the answer, the time it took, the tokens.
    last: dict[str, Any] = field(default_factory=dict)

    def next(
        self, picture: bytes, options: dict[str, str], outcome: str, heading_deg: float
    ) -> str | None:
        """Show the model the drawn picture and what may be picked; get its pick.

        ``options`` is label to words (candidates.options), ``outcome`` what the last pick
        did, ``heading_deg`` how far the chair has turned since the start (left positive).
        Returns one of the labels, or None if the model gave no usable answer.
        """
        from google.genai import types

        if self.client is None:
            self.client = client()
        way = "left" if heading_deg >= 0 else "right"
        text = "\n".join(
            [
                f"Task: {self.task}",
                f"Look {len(self.contents) // 2 + 1}.",
                f"Your last pick: {outcome}",
                f"Since the start the chair has turned {abs(heading_deg):.0f}° to the {way}.",
                f"Your last note: {self.last.get('note') or 'none yet'}",
                "You may pick:",
                *(f"- {label}: {words}" for label, words in options.items()),
            ]
        )
        look = types.Content(
            role="user",
            parts=[
                types.Part.from_bytes(data=picture, mime_type="image/jpeg"),
                types.Part.from_text(text=text),
            ],
        )
        started = time.perf_counter()
        response = self.client.models.generate_content(
            model=self.model,
            contents=[*self.contents, look],
            config=types.GenerateContentConfig(
                system_instruction=self.prompt_file.read_text(),
                thinking_config=types.ThinkingConfig(thinking_level=self.thinking),
                response_mime_type="application/json",
                # Only what is on offer in this picture can be answered.
                response_json_schema={
                    "type": "object",
                    "properties": {
                        "scene": {"type": "string"},
                        "choice": {"type": "string", "enum": list(options)},
                        "note": {"type": "string"},
                    },
                    "required": ["scene", "choice", "note"],
                },
            ),
        )
        ms = round((time.perf_counter() - started) * 1000.0)
        try:
            answer = json.loads(response.text or "")
        except ValueError:
            answer = {}
        # The model's own turn is kept whole (its thoughts' signatures included).
        reply = response.candidates[0].content if response.candidates else None
        self.contents += [look, reply or types.Content(role="model", parts=[types.Part(text="")])]
        usage = response.usage_metadata
        self.last = {
            "scene": answer.get("scene"),
            "choice": answer.get("choice"),
            "note": answer.get("note"),
            # Exactly what was sent with the picture, and what came back.
            "prompt": text,
            "raw": response.text,
            "ms": ms,
            "model": self.model,
            "thinking": self.thinking,
            "input_tokens": usage.prompt_token_count if usage else None,
            "output_tokens": usage.candidates_token_count if usage else None,
            "thinking_tokens": usage.thoughts_token_count if usage else None,
            "cached_tokens": (usage.cached_content_token_count or 0) if usage else None,
        }
        choice = self.last["choice"]
        return choice if choice in options else None


def client() -> Any:
    """The Gemini client. v1beta: the SDK's default API version does not serve Robotics-ER."""
    from google import genai
    from google.genai import types

    return genai.Client(http_options=types.HttpOptions(api_version="v1beta", timeout=40_000))


def drawn(jpeg: bytes, depth: Any = None) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """A recorded picture as a live run would show it: the paths on offer, the picture with
    them drawn, and what the obstacle sense made of it.

    ``depth`` is an obstacles.DepthObstacles; without one there are no obstacles and every
    path is offered whole.
    """
    import numpy as np
    from PIL import Image

    from fsd_ml import bench, candidates, chair
    from fsd_ml.pipeline.footprint import stop_distance_m

    setup = chair.load()
    seen, info = [], {"floor": None}
    if depth is not None:
        rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))
        camera = setup.camera
        seen, info = depth.find(rgb, camera.hfov_deg, camera.height_m, setup.lens_xy)
    # As a live run lays them (Session.candidates), at the bench's default settings.
    settings = bench.DEFAULTS
    speed = settings["drive_stick_pct"] / 100 * settings["v_full_mps"]
    stop_m = stop_distance_m(speed) + candidates.PLAN_SLACK_M
    paths = (
        candidates.fan(seen, setup, settings["path_clearance_m"], stop_m)
        if info["floor"] is not False
        else {}
    )
    return paths, candidates.draw(jpeg, paths, setup), info


def main() -> None:
    from fsd_ml import candidates, obstacles

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pictures", nargs="+", type=Path, help="recorded camera JPEGs")
    parser.add_argument("--say", default="go to the stairs", help="the task")
    parser.add_argument("--out", type=Path, default=Path("runs/gemini"))
    parser.add_argument("--no-ask", action="store_true", help="only draw; do not call Gemini")
    args = parser.parse_args()
    depth = obstacles.DepthObstacles() if obstacles.available() else None
    args.out.mkdir(parents=True, exist_ok=True)
    for source in args.pictures:
        paths, picture, info = drawn(source.read_bytes(), depth)
        saved = args.out / f"{source.parent.parent.name}-{source.stem}.picks.jpg"
        saved.write_bytes(picture)
        lengths = {label: round(len(path) * 0.1, 1) for label, path in paths.items()}
        print(f"{source}\n  floor: {info}\n  paths (m): {lengths}\n  {saved}")
        if not args.no_ask:
            pilot = GeminiPilot(args.say)
            choice = pilot.next(picture, candidates.options(paths), "none yet.", 0.0)
            print(f"  choice: {choice}  {json.dumps(pilot.last)}")


if __name__ == "__main__":
    main()
