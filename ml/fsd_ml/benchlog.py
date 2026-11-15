"""Print the bench's journal for reading: each run, how it ended, and what was said about it.

uv run python -m fsd_ml.benchlog          # the last ten runs
uv run python -m fsd_ml.benchlog 30
"""

from __future__ import annotations

import sys
from pathlib import Path

from fsd_ml.bench import Bench


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    count = int(argv[0]) if argv else 10
    bench = Bench(Path("runs/viewer/bench"))
    runs: dict[str, dict] = {}
    for entry in bench.journal(2000):
        run = entry.get("run")
        if entry["kind"] == "run":
            runs[run] = {"run": entry}
        elif run in runs:
            runs[run][entry["kind"]] = entry
    for name, parts in list(runs.items())[-count:]:
        started, result, said = parts["run"], parts.get("result"), parts.get("feedback")
        print(f"\n{name}  params {started['params']}")
        changed = {k: v for k, v in started["settings"].items()}
        print(f"  settings: {changed}")
        if result:
            print(
                f"  result: {result['outcome']} · {result['reason']} · {result['seconds']} s"
                f" · ended (ahead, left, turned) {result.get('ended')}"
            )
        else:
            print("  result: none recorded (still running, or the viewer was restarted)")
        if said:
            print(f"  feedback: [{said['rating']}] measured: {said['measured']!r} · {said['text']}")
        else:
            print("  feedback: none yet")


if __name__ == "__main__":
    main()
