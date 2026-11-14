"""Download model checkpoints into ml/weights/, which git ignores.

    uv run --group weights python -m fsd_ml.weights            # list
    uv run --group weights python -m fsd_ml.weights s2e        # download one

Check each model's licence before using it beyond the bake-off (docs/model.md).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

WEIGHTS_DIR = Path(__file__).resolve().parent.parent / "weights"

NAV_ZOO = "UCLA-VAIL/Navigation-Model-Zoo-Public"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    repo: str
    # Only these paths are fetched; None fetches the whole repository.
    include: tuple[str, ...] | None
    note: str


# Only entries whose repository and paths have been confirmed. The zoo also
# holds NoMaD, MIMIC and others; add them once their folder names are checked.
CHECKPOINTS: dict[str, Checkpoint] = {
    "flowpilot-goal-free": Checkpoint(
        NAV_ZOO, ("FlowPilot_Official/*",), "FlowPilot, older goal-free variant, 230M"
    ),
    "s2e": Checkpoint(NAV_ZOO, ("S2E/*",), "S2E, pretrained weights only, 95M"),
    "omnivla": Checkpoint("NHirose/omnivla-original", None, "OmniVLA, 8B; large download"),
    "omnivla-edge": Checkpoint("NHirose/omnivla-edge", None, "OmniVLA small variant"),
}


def download(name: str, weights_dir: Path = WEIGHTS_DIR) -> Path:
    if name not in CHECKPOINTS:
        raise KeyError(f"unknown checkpoint {name!r}; have {', '.join(sorted(CHECKPOINTS))}")
    # Imported here so listing, and everything else in fsd_ml, works without it.
    from huggingface_hub import snapshot_download

    checkpoint = CHECKPOINTS[name]
    target = weights_dir / name
    snapshot_download(
        repo_id=checkpoint.repo,
        allow_patterns=list(checkpoint.include) if checkpoint.include else None,
        local_dir=target,
    )
    return target


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("name", nargs="?", help="checkpoint to download; omit to list")
    args = parser.parse_args(argv)

    if args.name is None:
        for name, checkpoint in sorted(CHECKPOINTS.items()):
            print(f"{name:22} {checkpoint.note}")
        return
    print(download(args.name))


if __name__ == "__main__":
    main()
