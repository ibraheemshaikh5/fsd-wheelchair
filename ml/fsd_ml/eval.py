"""Replay recorded frames through several drivers and pack their answers for a blind judge.

    uv run --env-file .env python -m fsd_ml.eval ask --name bag --say "go to the brown paper bag" \\
        --driver remote:https://<modal-url> runs/console/live/<session> [...more sessions]

Every Nth frame a driver actually used in those sessions (with ``--looks``, only the frames a
pilot stood still and decided on) is shown, with the instruction, to each candidate: OmniVLA
(``--driver``) and the pilots in ``--models``. Those are the Claude pilot's variants and two
ways of asking Gemini: ``gemini-moves`` (the raw picture and the Claude pilot's move tools)
and ``gemini-paths`` (the picture with numbered paths drawn on it, fsd_ml/gemini_pilot.py;
its drawn picture is saved beside the frame as ``NNN.picks.jpg``). Each answer is reduced to
one first action in plain words and saved under ``runs/eval/<name>/``:

- ``answers.jsonl``: per frame, every model's action, latency and raw output (for us).
- ``judge.md``: per frame, the image path and the actions as A, B, C in a shuffled order,
  with no model names and no model reasoning (for the judge).
- ``key.json``: which letter was which model on each frame.

``score`` reads the judge's ``verdicts.json`` and prints accuracy and latency per model.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import random
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image

from fsd_ml import candidates, gemini_pilot, obstacles
from fsd_ml.drivers import Goal, Motion, make_driver
from fsd_ml.pilot import PROMPT_FILE, TOOLS, Call, Pilot

# name: (model, think first). Pick which to run with --models; omnivla is always asked.
PILOTS = {
    "haiku": ("claude-haiku-5-5", False),
    "sonnet": ("claude-sonnet-5-5", False),
    "haiku-think": ("claude-haiku-5-5", True),
    "sonnet-think": ("claude-sonnet-5-5", True),
}


GEMINI = ("gemini-moves", "gemini-paths")


def frames_used(sessions: list[Path], every: int, looks: bool = False) -> list[Path]:
    """Every ``every``-th distinct frame that a driving command used, oldest first.

    With ``looks``, only the frames a pilot was shown to decide on.
    """
    seen: list[Path] = []
    for session in sessions:
        for line in (session / "log.jsonl").read_text().splitlines():
            event = json.loads(line)
            frame = event.get("frame")
            if looks and "look" not in (event.get("model") or {}):
                continue
            if frame and (session / frame) not in seen:
                seen.append(session / frame)
    return seen[::every]


def path_action(path_xy: np.ndarray) -> str:
    """OmniVLA's path as one first action, read the way the console steers from it."""
    path = np.asarray(path_xy, dtype=float)
    ahead, left = path[min(4, len(path) - 1)]
    if math.hypot(ahead, left) < 0.15:
        return "stop"
    degrees = math.degrees(math.atan2(left, ahead))
    if abs(degrees) < 8:
        return f"forward {ahead:.1f} m"
    return f"turn {'left' if degrees > 0 else 'right'} {abs(degrees):.0f}° while moving forward"


def first_action(calls: list[Call]) -> tuple[str, str]:
    """A pilot's first tool call as an action, and its stated reason."""
    if not calls:
        return "no answer", ""
    call = calls[0]
    if call.name == "done":
        return (
            "stop: says it has arrived" if call.input.get("arrived") else "stop: gives up"
        ), call.why
    return call.says(), call.why


def gemini_moves(jpeg: bytes, say: str) -> dict:
    """Gemini asked the way the Claude pilot is: the raw picture, its prompt, its move tools."""
    from google.genai import types

    tools = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "parameters_json_schema": {
                k: v for k, v in tool["input_schema"].items() if k != "additionalProperties"
            },
        }
        for tool in TOOLS
    ]
    client = gemini_pilot.client()  # kept in a name: the SDK closes a client nothing holds
    started = time.perf_counter()
    response = client.models.generate_content(
        model=gemini_pilot.MODEL,
        contents=[
            types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
            f"Your task: {say}",
        ],
        config=types.GenerateContentConfig(
            system_instruction=PROMPT_FILE.read_text(),
            thinking_config=types.ThinkingConfig(thinking_level=gemini_pilot.THINKING),
            tools=[types.Tool(function_declarations=tools)],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            ),
        ),
    )
    ms = round((time.perf_counter() - started) * 1000)
    calls = [Call("", c.name, dict(c.args or {})) for c in response.function_calls or []]
    action, why = first_action(calls)
    return {"action": action, "ms": ms, "why": why}


def gemini_paths(jpeg: bytes, say: str, depth: obstacles.DepthObstacles | None) -> dict:
    """Gemini asked to pick from the paths drawn on the picture. Also gives that picture."""
    paths, picture, info = gemini_pilot.drawn(jpeg, depth)
    offered = candidates.options(paths)
    pilot = gemini_pilot.GeminiPilot(say)
    choice = pilot.next(picture, offered, "none yet: this is the first look.", 0.0)
    action = {None: "no answer", "S": "stop: says it has arrived", "X": "stop: gives up"}.get(
        choice, offered.get(choice, "").removesuffix(" on the spot")
    )
    return {
        "action": action,
        "ms": pilot.last.get("ms"),
        "why": f"{pilot.last.get('scene')} {pilot.last.get('note')}",
        "offered": [words for label, words in offered.items() if label.isdigit()],
        "floor": info,
        "picture": picture,
    }


def ask(args: argparse.Namespace) -> None:
    out = Path("runs/eval") / args.name
    (out / "frames").mkdir(parents=True, exist_ok=True)
    driver = make_driver(args.driver) if args.driver else None
    goal = Goal(instruction=args.say)
    rng = random.Random(0)
    answers, key, pack = [], {}, [f'# Task given to the chair: "{args.say}"\n']
    depth = None
    if "gemini-paths" in args.models and obstacles.available():
        depth = obstacles.DepthObstacles()
    used = frames_used([Path(s) for s in args.sessions], args.every, args.looks)
    for n, source in enumerate(used):
        image = out / "frames" / f"{n:03d}.jpg"
        shutil.copy(source, image)
        jpeg = image.read_bytes()
        rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))
        row: dict[str, dict] = {}
        if driver:
            started = time.perf_counter()
            path = driver.propose([rgb], goal, Motion())[0].path_xy
            row["omnivla"] = {
                "action": path_action(path),
                "ms": round((time.perf_counter() - started) * 1000),
                "path": np.round(path, 2).tolist(),
            }
        drawn = None
        for name in args.models:
            if name == "gemini-moves":
                row[name] = gemini_moves(jpeg, args.say)
            elif name == "gemini-paths":
                row[name] = gemini_paths(jpeg, args.say, depth)
                drawn = image.with_suffix(".picks.jpg")
                drawn.write_bytes(row[name].pop("picture"))
            else:
                model, thinking = PILOTS[name]
                pilot = Pilot(args.say, model=model, thinking=thinking)
                action, why = first_action(pilot.next(jpeg, ""))
                row[name] = {"action": action, "ms": pilot.last.get("ms"), "why": why}
        names = list(row)
        rng.shuffle(names)
        letters = dict(zip("ABCDEFGH", names, strict=False))
        key[f"{n:03d}"] = letters
        answers.append({"frame": f"{n:03d}", "source": str(source), **row})
        pack.append(f"## Frame {n:03d}\nImage: {image.resolve()}\n")
        if drawn:
            pack.append(
                f"The same image with guide lines and numbered paths drawn (solid: what would"
                f" be driven; dashed: where it leads): {drawn.resolve()}\n"
            )
        pack += [f"- {letter}: {row[name]['action']}" for letter, name in letters.items()]
        pack.append("")
        print(n, {k: v["action"] for k, v in row.items()})
    (out / "answers.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers))
    (out / "key.json").write_text(json.dumps(key, indent=1))
    (out / "judge.md").write_text("\n".join(pack))
    print(f"wrote {out}/judge.md ({len(answers)} frames)")


def score(args: argparse.Namespace) -> None:
    out = Path("runs/eval") / args.name
    key = json.loads((out / "key.json").read_text())
    verdicts = json.loads((out / "verdicts.json").read_text())
    answers = [json.loads(line) for line in (out / "answers.jsonl").read_text().splitlines()]
    totals: dict[str, list[float]] = {}
    for frame, grades in verdicts.items():
        for letter, grade in grades["grades"].items():
            totals.setdefault(key[frame][letter], []).append(float(grade))
    for name, grades in sorted(totals.items()):
        ms = [a[name]["ms"] for a in answers if a[name].get("ms")]
        print(
            f"{name:8s} score {sum(grades) / len(grades):.2f} over {len(grades)} frames"
            f" | median {sorted(ms)[len(ms) // 2]} ms"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("ask")
    p.add_argument("sessions", nargs="+")
    p.add_argument("--name", required=True)
    p.add_argument("--say", required=True)
    p.add_argument("--driver", help="remote:<url> to include OmniVLA")
    p.add_argument("--models", nargs="*", default=["haiku", "sonnet"], choices=[*PILOTS, *GEMINI])
    p.add_argument("--every", type=int, default=10)
    p.add_argument("--looks", action="store_true", help="only frames a pilot decided on")
    p.set_defaults(run=ask)
    s = sub.add_parser("score")
    s.add_argument("--name", required=True)
    s.set_defaults(run=score)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
