"""Read a recorded walk: GPS fixes and compass readings from sensors.jsonl, and the
phone-to-server clock offset from meta.json.

Lines are validated with the same models the service uses, so a bad line is
counted and skipped rather than trusted.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from drive_service.protocol import Gps, Heading

SENSORS_FILE = "sensors.jsonl"
META_FILE = "meta.json"
# Recording folders are named by their UTC start time, e.g. 20261114T150000Z.
SESSION_NAME = re.compile(r"^\d{8}T\d{6}Z$")


@dataclass(frozen=True, slots=True)
class Fix:
    fix_t: float
    recv_ms: float | None
    lat: float
    lon: float
    accuracy_m: float
    altitude_m: float | None
    speed_mps: float | None
    course_deg: float | None


@dataclass(frozen=True, slots=True)
class CompassReading:
    t: float
    magnetic_deg: float


@dataclass(frozen=True, slots=True)
class Session:
    fixes: tuple[Fix, ...]
    compass: tuple[CompassReading, ...]
    skipped_lines: int
    # Server clock = phone clock + this, estimated when the recording started.
    clock_offset_ms: float | None = None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _receive_time(record: dict) -> float | None:
    value = _number(record.pop("recv_ms", None))
    return value if value is not None and value > 0 else None


def _parse_line(line: str) -> Fix | CompassReading | None:
    """A fix or compass reading, None for other message types; raises on bad lines."""
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("not an object")
    kind = record.get("type")
    if kind not in ("gps", "heading"):
        return None
    recv_ms = _receive_time(record)
    if kind == "heading":
        heading = Heading.model_validate(record)
        return CompassReading(t=heading.t, magnetic_deg=heading.magnetic_deg)
    gps = Gps.model_validate(record)
    return Fix(
        fix_t=gps.fix_t,
        recv_ms=recv_ms,
        lat=gps.lat,
        lon=gps.lon,
        accuracy_m=gps.accuracy_m,
        altitude_m=gps.altitude_m,
        speed_mps=gps.speed_mps,
        course_deg=gps.course_deg,
    )


def parse_lines(lines: Iterable[str], clock_offset_ms: float | None = None) -> Session:
    """Fixes (one per fix_t, in order) and compass readings; bad lines are counted."""
    fixes: dict[float, Fix] = {}
    compass: list[CompassReading] = []
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            item = _parse_line(line)
        except (ValueError, TypeError, ValidationError):
            skipped += 1
            continue
        if isinstance(item, Fix):
            # The capture page resends its last fix after a reconnect; keep the first copy.
            fixes.setdefault(item.fix_t, item)
        elif isinstance(item, CompassReading):
            compass.append(item)
    return Session(
        fixes=tuple(sorted(fixes.values(), key=lambda fix: fix.fix_t)),
        compass=tuple(sorted(compass, key=lambda reading: reading.t)),
        skipped_lines=skipped,
        clock_offset_ms=clock_offset_ms,
    )


def read_clock_offset(session_dir: Path) -> float | None:
    """The clock offset recorded at the start, or None if unsynced or unreadable."""
    try:
        meta = json.loads((session_dir / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    clock = meta.get("clock") if isinstance(meta, dict) else None
    return _number(clock.get("offset_ms")) if isinstance(clock, dict) else None


def load_session(session_dir: Path) -> Session:
    offset = read_clock_offset(session_dir)
    path = session_dir / SENSORS_FILE
    if not path.is_file():
        return Session(fixes=(), compass=(), skipped_lines=0, clock_offset_ms=offset)
    with path.open(encoding="utf-8") as lines:
        return parse_lines(lines, offset)


def latest_session(record_dir: Path) -> Path | None:
    """The newest recording folder that has sensor data, judged by its name."""
    if not record_dir.is_dir():
        return None
    sessions = [
        path
        for path in record_dir.iterdir()
        if path.is_dir() and SESSION_NAME.match(path.name) and (path / SENSORS_FILE).is_file()
    ]
    return max(sessions, key=lambda path: path.name, default=None)
