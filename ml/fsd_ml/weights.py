"""Download model checkpoints into ml/weights/, which git ignores.

    uv run --group weights python -m fsd_ml.weights            # list
    uv run --group weights python -m fsd_ml.weights s2e        # download one

Check each model's licence before using it beyond the bake-off (docs/model.md).
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

# FSD_WEIGHTS_DIR moves the folder, for machines where this one is read-only.
WEIGHTS_DIR = Path(
    os.environ.get("FSD_WEIGHTS_DIR") or Path(__file__).resolve().parent.parent / "weights"
)

NAV_ZOO = "UCLA-VAIL/Navigation-Model-Zoo-Public"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    repo: str
    # Only these paths are fetched; None fetches the whole repository.
    include: tuple[str, ...] | None
    note: str


# Only entries whose repository and paths have been confirmed.
CHECKPOINTS: dict[str, Checkpoint] = {
    "flowpilot": Checkpoint(
        "UCLA-VAIL/Visual-Navigation-Model-Checkpoints",
        (
            "flowpilot-dst-small/flowpilot_dst_fastvit_t12.onnx",
            "flowpilot-dst-small/flowpilot_dst_fastvit_t12.metadata.json",
            "flowpilot-dst-small/flowpilot_dst_fastvit_t12.inputs.npz",
        ),
        "FlowPilot small (flowpilot-dst-small), 21M parameters",
    ),
    "flowpilot-goal-free": Checkpoint(
        NAV_ZOO, ("FlowPilot_Official/*",), "FlowPilot, older goal-free variant, 230M"
    ),
    "s2e": Checkpoint(
        NAV_ZOO, ("S2E/s2e.onnx", "S2E/model_info.yaml"), "S2E, pretrained weights only, 382 MB"
    ),
    "nomad": Checkpoint(NAV_ZOO, ("NoMaD_GL_Official/*",), "NoMaD, goal-free, 111 MB"),
    "mbra": Checkpoint(NAV_ZOO, ("MBRA_PG_Official/*",), "MBRA, point goal, 254 MB"),
    "citywalker": Checkpoint(NAV_ZOO, ("CityWalker_PG_Official/*",), "CityWalker, 806 MB"),
    "mimic": Checkpoint(NAV_ZOO, ("MIMIC/*",), "MIMIC, goal-free; its code needs urbansim"),
    "omnivla": Checkpoint("NHirose/omnivla-original", None, "OmniVLA, 8B; large download"),
    "omnivla-edge": Checkpoint("NHirose/omnivla-edge", None, "OmniVLA small variant"),
}


def download(name: str, weights_dir: Path = WEIGHTS_DIR) -> Path:
    if name not in CHECKPOINTS:
        raise KeyError(f"unknown checkpoint {name!r}; have {', '.join(sorted(CHECKPOINTS))}")
    # Imported here so listing, and everything else in fsd_ml, works without it.
    from huggingface_hub import snapshot_download

    checkpoint = CHECKPOINTS[name]
    # Zoo models share one folder, each under its own sub-folder as in the repository.
    target = weights_dir / ("nav-zoo" if checkpoint.repo == NAV_ZOO else name)
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
