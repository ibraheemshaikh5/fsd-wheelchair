"""Obstacles and the chair's real motion from an iPhone's lidar.

The native sender (ios/ChairSender) posts, about ten times a second, a small depth map
measured by the phone's lidar and the phone's own position from ARKit. That replaces two
guesses made with the camera alone: how far things are, and how far the chair has moved.

One packet, as posted to ``/api/lidar``::

    [uint32 little-endian: header length][header JSON][depth: uint16 millimetres, row by row]

    header: {"t": ms since the epoch, "w": px, "h": px,
             "fx", "fy", "cx", "cy": the depth map's own lens numbers, in its pixels,
             "pose": 16 numbers, ARKit's camera-to-world matrix, column by column,
             "tracking": "normal" | "limited" | "none"}

ARKit's frames: the camera looks along its own -z with +x to the right of the sensor and
+y up; the world's +y is straight up, whichever way the phone is mounted. So gravity is
known exactly and nothing here depends on how the phone is turned in its holder.
"""

from __future__ import annotations

import json
import math
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from fsd_ml.pipeline.types import Obstacle

_LENGTH = struct.Struct("<I")
MAX_PACKET_BYTES = 600_000
MAX_SIDE_PX = 512
# A packet older than this is not the present.
FRESH_S = 0.6
CELL_M = 0.12
RANGE_M = 4.5
# Lidar is clean enough to call something an obstacle from this little above the floor.
FLOOR_BAND_M = 0.05
TOP_M = 1.4
MIN_POINTS = 3
KEEP = 150
# The floor must turn up about where the camera's height says, or it is not the floor.
FLOOR_TOLERANCE_M = 0.25


class LidarError(ValueError):
    """A packet that cannot be used."""


@dataclass(frozen=True, slots=True)
class Packet:
    t_ms: float
    # Metres, shape (h, w); 0 where the lidar had no return.
    depth_m: np.ndarray
    fx: float
    fy: float
    cx: float
    cy: float
    # Camera to world, 4x4.
    pose: np.ndarray
    tracking: str
    received_s: float


def parse(body: bytes, received_s: float | None = None) -> Packet:
    if not _LENGTH.size <= len(body) <= MAX_PACKET_BYTES:
        raise LidarError("packet is empty or too large")
    (header_length,) = _LENGTH.unpack_from(body)
    start = _LENGTH.size + header_length
    if header_length > 4096 or start > len(body):
        raise LidarError("bad header length")
    try:
        header = json.loads(body[_LENGTH.size : start])
        width, height = int(header["w"]), int(header["h"])
        lens = [float(header[name]) for name in ("fx", "fy", "cx", "cy")]
        pose = np.asarray(header["pose"], dtype=np.float64)
        t_ms, tracking = float(header["t"]), str(header.get("tracking", "normal"))
    except (ValueError, KeyError, TypeError) as error:
        raise LidarError(f"bad header: {error}") from error
    if not (0 < width <= MAX_SIDE_PX and 0 < height <= MAX_SIDE_PX):
        raise LidarError("bad depth size")
    if len(body) - start != width * height * 2:
        raise LidarError("depth does not match its size")
    if pose.shape != (16,) or not np.isfinite(pose).all() or min(lens[:2]) <= 0:
        raise LidarError("bad pose or lens numbers")
    depth = np.frombuffer(body, dtype="<u2", offset=start).reshape(height, width) / 1000.0
    return Packet(
        t_ms,
        depth,
        *lens,
        pose.reshape(4, 4).T,  # sent column by column
        tracking,
        time.monotonic() if received_s is None else received_s,
    )


def _level_axes(pose: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """The chair's "ahead" and "left" along the floor, in the world: where the camera looks."""
    looking = -pose[:3, 2]
    ahead = np.array([looking[0], 0.0, looking[2]])
    length = np.linalg.norm(ahead)
    if length < 0.2:  # looking nearly straight up or down
        return None
    ahead /= length
    left = np.cross([0.0, 1.0, 0.0], ahead)
    return ahead, left


def obstacles_from_lidar(
    packet: Packet, camera_height_m: float, lens_xy: tuple[float, float] = (0.0, 0.0)
) -> tuple[list[Obstacle], dict[str, Any]]:
    """What stands on the floor, in the chair's frame (metres ahead, metres left)."""
    axes = _level_axes(packet.pose)
    if axes is None:
        return [], {"floor": False, "source": "lidar", "why": "camera is not looking ahead"}
    ahead_axis, left_axis = axes
    depth = packet.depth_m
    height, width = depth.shape
    us, vs = np.meshgrid(np.arange(width) + 0.5, np.arange(height) + 0.5)
    valid = (depth > 0.15) & (depth < 6.0)
    z = depth[valid]
    # In the camera's own frame: right, up, and (negative) the way it looks.
    points = np.column_stack(
        [(us[valid] - packet.cx) / packet.fx * z, -(vs[valid] - packet.cy) / packet.fy * z, -z]
    )
    world = points @ packet.pose[:3, :3].T  # turned level; still measured from the lens
    up = world[:, 1]
    if len(up) < 200:
        return [], {"floor": False, "source": "lidar", "why": "too few lidar returns"}
    # The floor is the lowest thing that a good share of the returns agree on.
    floor = float(np.percentile(up, 4))
    if abs(floor + camera_height_m) > FLOOR_TOLERANCE_M:
        return [], {
            "floor": False,
            "source": "lidar",
            "why": f"floor seen {-floor:.2f} m below the lens, not {camera_height_m:.2f} m",
        }
    above = up - floor
    ahead = world @ ahead_axis
    left = world @ left_axis
    standing = (above > FLOOR_BAND_M) & (above < TOP_M) & (ahead > 0.1) & (ahead < RANGE_M)
    cells: dict[tuple[int, int], list[float]] = {}
    for a, b in zip(ahead[standing], left[standing], strict=True):
        cell = cells.setdefault((int(a // CELL_M), int(b // CELL_M)), [0, 0.0, 0.0])
        cell[0] += 1
        cell[1] += a
        cell[2] += b
    filled = sorted((c for c in cells.values() if c[0] >= MIN_POINTS), key=lambda c: c[1] / c[0])
    found = [
        Obstacle(
            f"o{i}",
            "object",
            round(float(total_a / n) + lens_xy[0], 3),
            round(float(total_b / n) + lens_xy[1], 3),
            radius_m=CELL_M * 0.6,
        )
        for i, (n, total_a, total_b) in enumerate(filled[:KEEP])
    ]
    return found, {"floor": True, "source": "lidar", "lens_height_m": round(-floor, 3)}


def motion_between(
    earlier: Packet, later: Packet, lens_from_pivot: tuple[float, float] = (0.0, 0.0)
) -> tuple[float, float] | None:
    """The chair's forward speed (m/s) and turn rate (rad/s, positive left) between two packets.

    The phone is not over the drive wheels, so when the chair turns the phone swings
    sideways and forward; ``lens_from_pivot`` (ahead, left of the wheels' middle) takes that
    swing back out, leaving the speed of the wheels' middle itself.
    """
    dt = (later.t_ms - earlier.t_ms) / 1000.0
    if not 0.01 < dt < 1.0:
        return None
    before, after = _level_axes(earlier.pose), _level_axes(later.pose)
    if before is None or after is None:
        return None
    # Heading turns about straight-up; from above, ARKit's x and z give the angle.
    turn = math.atan2(
        float(np.cross(before[0], after[0]) @ [0.0, 1.0, 0.0]), float(before[0] @ after[0])
    )
    step = later.pose[:3, 3] - earlier.pose[:3, 3]
    middle = before[0] + after[0]
    middle /= np.linalg.norm(middle)
    rate = turn / dt
    speed = float(step @ middle) / dt + rate * lens_from_pivot[1]
    return speed, rate


class LidarFeed:
    """The newest lidar packets, and what they say about obstacles and motion."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._latest: Packet | None = None
        self._previous: Packet | None = None
        self.count = 0
        self.problem: str | None = None

    def take(self, body: bytes) -> None:
        try:
            packet = parse(body)
        except LidarError as error:
            self.problem = str(error)
            raise
        with self._guard:
            self._previous, self._latest = self._latest, packet
            self.count += 1
            self.problem = None

    def latest(self) -> Packet | None:
        """The newest packet, if it is fresh and the phone knows where it is."""
        with self._guard:
            packet = self._latest
        if packet is None or time.monotonic() - packet.received_s > FRESH_S:
            return None
        return packet if packet.tracking == "normal" else None

    def motion(
        self, lens_from_pivot: tuple[float, float] = (0.0, 0.0)
    ) -> tuple[float, float] | None:
        with self._guard:
            earlier, later = self._previous, self._latest
        if earlier is None or later is None or time.monotonic() - later.received_s > FRESH_S:
            return None
        if earlier.tracking != "normal" or later.tracking != "normal":
            return None
        return motion_between(earlier, later, lens_from_pivot)
