"""Lidar packets: read them, map the floor's obstacles, and work out the chair's motion."""

import json
import math
import struct

import numpy as np
import pytest

from fsd_ml.lidar import LidarError, LidarFeed, motion_between, obstacles_from_lidar, parse

W, H, F = 128, 96, 100.0
HEIGHT_M = 0.4


def pose(x=0.0, z=0.0, yaw=0.0, roll_deg=0.0) -> list[float]:
    """Camera-to-world, column by column: at (x, HEIGHT_M, z), level, turned left by ``yaw``.

    With no yaw the camera looks along the world's -z. ``roll_deg`` turns the phone in its
    holder about the way it looks.
    """
    c, s = math.cos(yaw), math.sin(yaw)
    turn = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    r = math.radians(roll_deg)
    roll = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    matrix = np.eye(4)
    matrix[:3, :3] = turn @ roll
    matrix[:3, 3] = [x, HEIGHT_M, z]
    return matrix.T.reshape(-1).tolist()


def scene(box_left_m: float | None, roll_deg: float = 0.0) -> np.ndarray:
    """Depth for a level camera over a floor, with maybe a box 1.5 m ahead, seen by ray casting."""
    r = math.radians(roll_deg)
    depth = np.zeros((H, W))
    for v in range(H):
        for u in range(W):
            ray = np.array([(u + 0.5 - W / 2) / F, -(v + 0.5 - H / 2) / F, -1.0])
            ray = np.array(
                [
                    ray[0] * math.cos(r) - ray[1] * math.sin(r),
                    ray[0] * math.sin(r) + ray[1] * math.cos(r),
                    ray[2],
                ]
            )
            best = 5.0 if ray[1] >= -1e-6 else min(5.0, HEIGHT_M / -ray[1])
            if box_left_m is not None:
                left, up = -ray[0] * 1.5, HEIGHT_M + ray[1] * 1.5
                if abs(left - box_left_m) < 0.2 and 0 < up < 0.5 and best > 1.5:
                    best = 1.5
            depth[v, u] = best
    return depth


def packet(depth: np.ndarray, t=1000.0, tracking="normal", **where) -> bytes:
    header = json.dumps(
        {
            "t": t,
            "w": W,
            "h": H,
            "fx": F,
            "fy": F,
            "cx": W / 2,
            "cy": H / 2,
            "pose": pose(**where),
            "tracking": tracking,
        }
    ).encode()
    return struct.pack("<I", len(header)) + header + (depth * 1000).astype("<u2").tobytes()


def test_a_box_on_the_floor_is_mapped_where_it_stands():
    found, info = obstacles_from_lidar(parse(packet(scene(0.4))), HEIGHT_M)
    assert info["floor"] and abs(info["lens_height_m"] - HEIGHT_M) < 0.03
    assert found and all(1.3 < o.x < 1.7 and 0.15 < o.y < 0.65 for o in found)
    assert obstacles_from_lidar(parse(packet(scene(None))), HEIGHT_M)[0] == []


def test_it_does_not_matter_which_way_up_the_phone_is_mounted():
    turned = parse(packet(scene(0.4, roll_deg=90.0), roll_deg=90.0))
    found, info = obstacles_from_lidar(turned, HEIGHT_M)
    assert info["floor"]
    assert found and all(1.3 < o.x < 1.7 and 0.15 < o.y < 0.65 for o in found)


def test_a_floor_in_the_wrong_place_is_not_trusted():
    found, info = obstacles_from_lidar(parse(packet(scene(None))), camera_height_m=1.2)
    assert found == [] and info["floor"] is False and "below the lens" in info["why"]


def test_a_covered_lidar_reports_no_floor():
    covered = np.full((H, W), 0.05)
    assert obstacles_from_lidar(parse(packet(covered)), HEIGHT_M)[1]["floor"] is False


def test_driving_forward_and_turning_left_are_read_from_the_pose():
    flat = scene(None)
    start = parse(packet(flat, t=1000.0))
    ahead = parse(packet(flat, t=1100.0, z=-0.05))  # the camera looks along -z
    speed, turn = motion_between(start, ahead)
    assert abs(speed - 0.5) < 1e-6 and abs(turn) < 1e-6
    turned = parse(packet(flat, t=1100.0, yaw=0.05))
    speed, turn = motion_between(start, turned)
    assert abs(turn - 0.5) < 1e-6 and abs(speed) < 1e-6


def test_the_phones_swing_about_the_wheels_is_not_counted_as_driving():
    # The chair turns on the spot; a phone 0.2 m left of the wheels' middle swings backward.
    flat, yaw = scene(None), 0.05
    start = parse(packet(flat, t=1000.0))
    swung = parse(packet(flat, t=1100.0, yaw=yaw, z=0.2 * yaw))
    speed, _ = motion_between(start, swung, lens_from_pivot=(0.6, 0.2))
    assert abs(speed) < 0.02


@pytest.mark.parametrize(
    "body",
    [b"", b"\x05\x00\x00\x00{}", packet(np.ones((H, W)))[:-10], struct.pack("<I", 9999) + b"{}"],
)
def test_bad_packets_are_refused(body):
    with pytest.raises(LidarError):
        parse(body)


def test_the_feed_only_offers_fresh_packets_from_a_phone_that_knows_where_it_is():
    feed = LidarFeed()
    assert feed.latest() is None and feed.motion() is None
    feed.take(packet(scene(None), t=1000.0))
    feed.take(packet(scene(None), t=1100.0, z=-0.05))
    assert feed.latest() is not None
    assert abs(feed.motion()[0] - 0.5) < 1e-6
    feed.take(packet(scene(None), t=1200.0, tracking="limited"))
    assert feed.latest() is None and feed.motion() is None
