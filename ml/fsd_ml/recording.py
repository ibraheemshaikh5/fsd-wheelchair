"""Read a drive recorded by the driving service.

The folder layout is the contract between the service and this project (see
docs/phone-protocol.md, "Recordings"). Nothing here imports the service.

    20261114T150000Z/
      meta.json
      frames/000001.jpg ...
      frames.jsonl     {n, file, seq, t, w, h, recv_ms} per frame
      sensors.jsonl    every gps/heading/imu message as received, plus recv_ms

Times are the phone's capture clock in milliseconds.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


class RecordingError(Exception):
    """The folder is not a readable recording."""


@dataclass(frozen=True, slots=True)
class FrameRef:
    """One camera frame on disk. The image is only read by :meth:`Recording.image`."""

    n: int
    path: Path
    seq: int
    t_ms: float
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Gps:
    t_ms: float
    lat: float
    lon: float
    accuracy_m: float
    speed_mps: float | None
    course_deg: float | None


@dataclass(frozen=True, slots=True)
class Heading:
    t_ms: float
    magnetic_deg: float
    accuracy_deg: float | None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RecordingError(f"missing {path.name} in {path.parent}")
    rows = []
    with path.open() as lines:
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise RecordingError(f"{path.name} line {number} is not JSON") from error
    return rows


def _latest_at[T: (Gps, Heading)](readings: list[T], times: list[float], t_ms: float) -> T | None:
    """The newest reading captured at or before ``t_ms``; never one from the future."""
    index = bisect.bisect_right(times, t_ms)
    return readings[index - 1] if index else None


class Recording:
    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        meta_path = self.folder / "meta.json"
        if not meta_path.is_file():
            raise RecordingError(f"no meta.json in {self.folder}")
        self.meta: dict[str, Any] = json.loads(meta_path.read_text())

        self.frames: list[FrameRef] = sorted(
            (
                FrameRef(
                    n=row["n"],
                    path=self.folder / row["file"],
                    seq=row["seq"],
                    t_ms=row["t"],
                    width=row["w"],
                    height=row["h"],
                )
                for row in _read_jsonl(self.folder / "frames.jsonl")
            ),
            key=lambda frame: frame.t_ms,
        )

        self.gps: list[Gps] = []
        self.headings: list[Heading] = []
        # Raw IMU rows: [capture ms, ax, ay, az (m/s^2, with gravity), rx, ry, rz (deg/s)].
        imu_rows: list[list[float]] = []
        for row in _read_jsonl(self.folder / "sensors.jsonl"):
            kind = row.get("type")
            if kind == "gps":
                self.gps.append(
                    Gps(
                        # The fix can be older than the message that carried it.
                        t_ms=min(row["fix_t"], row["t"]),
                        lat=row["lat"],
                        lon=row["lon"],
                        accuracy_m=row["accuracy_m"],
                        speed_mps=row.get("speed_mps"),
                        course_deg=row.get("course_deg"),
                    )
                )
            elif kind == "heading":
                self.headings.append(
                    Heading(row["t"], row["magnetic_deg"], row.get("accuracy_deg"))
                )
            elif kind == "imu":
                imu_rows.extend([row["t"] + s[0], *s[1:]] for s in row["samples"])

        self.gps.sort(key=lambda fix: fix.t_ms)
        self.headings.sort(key=lambda heading: heading.t_ms)
        self._gps_times = [fix.t_ms for fix in self.gps]
        self._heading_times = [heading.t_ms for heading in self.headings]
        imu = np.array(imu_rows, dtype=np.float64).reshape(-1, 7)
        self.imu = imu[np.argsort(imu[:, 0], kind="stable")]

    @property
    def duration_s(self) -> float:
        if len(self.frames) < 2:
            return 0.0
        return (self.frames[-1].t_ms - self.frames[0].t_ms) / 1000.0

    @property
    def fps(self) -> float:
        return (len(self.frames) - 1) / self.duration_s if self.duration_s else 0.0

    def image(self, frame: FrameRef) -> np.ndarray:
        """The frame as RGB, shape (height, width, 3), uint8."""
        with Image.open(frame.path) as image:
            return np.asarray(image.convert("RGB"))

    def gps_at(self, t_ms: float) -> Gps | None:
        return _latest_at(self.gps, self._gps_times, t_ms)

    def heading_at(self, t_ms: float) -> Heading | None:
        return _latest_at(self.headings, self._heading_times, t_ms)

    def imu_between(self, start_ms: float, end_ms: float) -> np.ndarray:
        """IMU rows with start_ms < capture time <= end_ms."""
        times = self.imu[:, 0]
        return self.imu[(times > start_ms) & (times <= end_ms)]

    def windows(self, size: int, stride: int = 1) -> Iterator[list[FrameRef]]:
        """Runs of ``size`` consecutive frames, oldest first: what a model sees at one step."""
        if size < 1 or stride < 1:
            raise ValueError("size and stride must be at least 1")
        for end in range(size, len(self.frames) + 1, stride):
            yield self.frames[end - size : end]
