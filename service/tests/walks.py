"""Synthetic walks for the walk report tests: fixes, compass readings and sessions."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

from helpers import gps_dict

START_MS = 1_700_000_000_000.0
START_LAT = 38.9869
START_LON = -76.9426
# Metres per degree of latitude on the spherical Earth geo.py uses.
M_PER_DEG_LAT = 6_371_000.0 * math.pi / 180.0
MOVING_MPS = 0.5
FIXED_DECLINATION = -12.0
SESSION_NAME = "20261114T150000Z"


def fixed_declination(lat: float, lon: float, altitude_m: float | None, when: object) -> float:
    return FIXED_DECLINATION


def line(message: dict, recv_ms: float | None = None) -> str:
    return json.dumps({"recv_ms": recv_ms if recv_ms is not None else message["t"], **message})


def shifted(messages: list[dict], by_s: float) -> list[dict]:
    """The same messages, later by ``by_s``."""
    ms = by_s * 1000.0
    return [
        {**m, "t": m["t"] + ms, **({"fix_t": m["fix_t"] + ms} if "fix_t" in m else {})}
        for m in messages
    ]


def walk_north(seconds: int, speed_mps: float = 1.4, every_s: float = 1.0) -> list[dict]:
    fixes = []
    for i in range(int(seconds / every_s) + 1):
        t = START_MS + i * every_s * 1000.0
        lat = START_LAT + (speed_mps * i * every_s) / M_PER_DEG_LAT
        fixes.append(
            gps_dict(t=t, fix_t=t, lat=lat, lon=START_LON, speed_mps=speed_mps, course_deg=0.0)
        )
    return fixes


def at_rest(
    seconds: int, every_s: float = 5.0, jitter_m: float = 0.0, speed: float | None = None
) -> list[dict]:
    """Standing still; ``jitter_m`` scatters each fix (seeded, so tests repeat)."""
    rng = random.Random(7)
    fixes = []
    for i in range(int(seconds / every_s) + 1):
        t = START_MS + i * every_s * 1000.0
        north_m, east_m = rng.gauss(0, jitter_m), rng.gauss(0, jitter_m)
        lat = START_LAT + north_m / M_PER_DEG_LAT
        lon = START_LON + east_m / (M_PER_DEG_LAT * math.cos(math.radians(START_LAT)))
        fixes.append(gps_dict(t=t, fix_t=t, lat=lat, lon=lon, speed_mps=speed, course_deg=None))
    return fixes


def write_session(
    root: Path, name: str, messages: list[dict], clock_offset_ms: float | None = 0.0
) -> Path:
    session = root / name
    session.mkdir()
    lines = [line(m, recv_ms=m["t"] + 500.0) for m in messages]
    (session / "sensors.jsonl").write_text("\n".join(lines) + "\n")
    clock = None if clock_offset_ms is None else {"offset_ms": clock_offset_ms, "rtt_ms": 10.0}
    (session / "meta.json").write_text(json.dumps({"protocol": 1, "clock": clock}))
    return session
