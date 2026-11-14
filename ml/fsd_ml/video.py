"""Put any video into the one form the models are fed: the standardizing step.

Whatever comes in (any size, frame rate, orientation or container) leaves as
numbered JPEG frames that are:

- **20 frames a second**, evenly spaced, which is the camera rate the pipeline
  and the models' frame histories assume;
- **16:9 landscape**, by cutting the middle out of anything taller or wider,
  never by squashing, so shapes keep their proportions;
- **640 x 360**, upright (a phone's rotation flag is applied).

Each model then resizes these to its own input size itself. Needs ``ffmpeg``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

FPS = 20
WIDTH, HEIGHT = 640, 360
ASPECT = WIDTH / HEIGHT


class VideoError(Exception):
    """The video could not be read, in words for the person who supplied it."""


@dataclass(frozen=True, slots=True)
class Conversion:
    """What a video was, and what was done to it."""

    source_width: int
    source_height: int
    source_fps: float
    duration_s: float
    frames: int
    # Fraction of the picture kept after cutting to 16:9; 1.0 means nothing was cut.
    kept: float
    # Set when the result will mislead a model, with what to do about it.
    warning: str | None

    def summary(self) -> str:
        was = f"{self.source_width}×{self.source_height} at {self.source_fps:g} fps"
        cut = "" if self.kept > 0.99 else f", middle {self.kept:.0%} kept"
        return f"{was} → {WIDTH}×{HEIGHT} at {FPS} fps{cut}"

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "summary": self.summary()}


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise VideoError(f"{name} is not installed; install it with: brew install ffmpeg")
    return path


def probe(video: Path) -> tuple[int, int, float, float]:
    """(width, height, frames per second, seconds) as the video is displayed."""
    done = subprocess.run(
        [
            _tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,duration:stream_side_data=rotation"
            ":stream_tags=rotate:format=duration",
            "-of", "json", str(video),
        ],
        capture_output=True,
        text=True,
    )  # fmt: skip
    try:
        described = json.loads(done.stdout)
        stream = described["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (ValueError, KeyError, IndexError) as error:
        raise VideoError(f"could not read {video.name}: not a video") from error

    rotation = 0.0
    for side in stream.get("side_data_list", []):
        rotation = float(side.get("rotation", rotation))
    rotation = float(stream.get("tags", {}).get("rotate", rotation))
    if round(abs(rotation)) % 180 == 90:
        # Stored on its side; players and ffmpeg turn it upright.
        width, height = height, width

    try:
        fps = float(Fraction(stream.get("avg_frame_rate", "0")))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = stream.get("duration") or described.get("format", {}).get("duration") or 0.0
    return width, height, fps, float(duration)


def standardize(video: Path, out_dir: Path) -> Conversion:
    """Write ``video`` into ``out_dir`` as 000001.jpg, ... in the standard form."""
    width, height, fps, duration = probe(video)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # Cut to 16:9 around the centre, then resize. ffmpeg has already turned the picture upright.
    cut = f"crop='min(iw,ih*{WIDTH}/{HEIGHT})':'min(ih,iw*{HEIGHT}/{WIDTH})'"
    done = subprocess.run(
        [
            _tool("ffmpeg"), "-loglevel", "error", "-i", str(video),
            "-vf", f"fps={FPS},{cut},scale={WIDTH}:{HEIGHT}",
            "-q:v", "4", str(out_dir / "%06d.jpg"),
        ],
        capture_output=True,
        text=True,
    )  # fmt: skip
    frames = len(list(out_dir.glob("*.jpg")))
    if done.returncode != 0 or frames == 0:
        message = done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "no frames"
        raise VideoError(f"could not read {video.name}: {message}")

    shape = width / height
    kept = min(shape, ASPECT) / max(shape, ASPECT)
    warning = None
    if shape < 1.0:
        warning = (
            f"This is a portrait video, so only the middle {kept:.0%} of it fits a landscape "
            "frame. Record in landscape for the models to see the whole view."
        )
    elif fps and fps < FPS - 1:
        warning = (
            f"This video has {fps:g} frames a second; frames are repeated to reach {FPS}, "
            "which models that read motion can mistake for stopping and starting."
        )
    return Conversion(width, height, round(fps, 2), round(duration, 2), frames, kept, warning)
