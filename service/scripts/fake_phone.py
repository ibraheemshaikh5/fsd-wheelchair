"""Pretend to be the chair's phone: stream synthetic frames, GPS, compass, IMU.

Usage (from service/, with the service running):

    uv run python -m scripts.fake_phone --url ws://127.0.0.1:8000/ws/phone --seconds 30

Frames are generated images with a frame counter; GPS walks a small loop
around a start point at walking pace. Useful for developing the viewer and
recorder without the iPhone, and as an end-to-end smoke test.

Like the capture page it sends hello first, then a ping straight away and once a
second after that. The service treats nothing as fresh until a ping carries a
measured round trip and offset, so sensors_ok turns true about a second in.
Connecting replaces any other phone (the older socket is closed with 4410).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import math
import time
from collections.abc import Iterator
from dataclasses import dataclass

from PIL import Image, ImageDraw

from drive_service.protocol import FrameHeader, encode_frame

log = logging.getLogger("fake_phone")

DEFAULT_URL = "ws://127.0.0.1:8000/ws/phone"
START_LAT = 38.9869
START_LON = -76.9426
LOOP_RADIUS_M = 20.0
WALK_SPEED_MPS = 1.2
METERS_PER_DEG_LAT = 111_320.0
JPEG_QUALITY = 70
GRAVITY = 9.81


@dataclass(frozen=True, slots=True)
class Pose:
    lat: float
    lon: float
    course_deg: float


def pose_at(elapsed_s: float) -> Pose:
    """Position on a circle walked counter-clockwise from due east of the centre."""
    angle = WALK_SPEED_MPS * elapsed_s / LOOP_RADIUS_M
    north_m = LOOP_RADIUS_M * math.sin(angle)
    east_m = LOOP_RADIUS_M * math.cos(angle)
    meters_per_deg_lon = METERS_PER_DEG_LAT * math.cos(math.radians(START_LAT))
    # Counter-clockwise from due east: heading north at the start, west a quarter
    # of the way round. Compass bearings run clockwise, hence the minus sign.
    return Pose(
        lat=START_LAT + north_m / METERS_PER_DEG_LAT,
        lon=START_LON + east_m / meters_per_deg_lon,
        course_deg=(-math.degrees(angle)) % 360.0,
    )


def make_jpeg(seq: int, width: int, height: int) -> bytes:
    shade = (seq * 7) % 255
    image = Image.new("RGB", (width, height), (shade, 80, 255 - shade))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, height * 2 // 3, width, height), fill=(90, 90, 90))
    draw.text((16, 16), f"fake phone frame {seq}", fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def hello(now_ms: float, width: int, height: int, fps: float) -> str:
    return json.dumps(
        {
            "v": 1,
            "type": "hello",
            "t": now_ms,
            "user_agent": "fake_phone.py",
            "camera": {"label": "synthetic", "width": width, "height": height, "fps": fps},
            "mount": {"height_m": 1.1, "pitch_deg": -10.0},
        }
    )


def gps(now_ms: float, pose: Pose) -> str:
    return json.dumps(
        {
            "v": 1,
            "type": "gps",
            "t": now_ms,
            "lat": pose.lat,
            "lon": pose.lon,
            "accuracy_m": 4.0,
            "altitude_m": 40.0,
            "speed_mps": WALK_SPEED_MPS,
            "course_deg": pose.course_deg,
            "fix_t": now_ms,
        }
    )


def heading(now_ms: float, pose: Pose) -> str:
    return json.dumps(
        {
            "v": 1,
            "type": "heading",
            "t": now_ms,
            "magnetic_deg": (pose.course_deg + 11.0) % 360.0,
            "accuracy_deg": 15.0,
        }
    )


def imu(now_ms: float) -> str:
    samples = [[i * 16.0, 0.0, 0.0, GRAVITY, 0.0, 0.0, 0.0] for i in range(6)]
    return json.dumps({"v": 1, "type": "imu", "t": now_ms, "samples": samples})


def ping(now_ms: float, rtt_ms: float | None, offset_ms: float | None) -> str:
    return json.dumps(
        {"v": 1, "type": "ping", "t": now_ms, "rtt_ms": rtt_ms, "offset_ms": offset_ms}
    )


def frame(seq: int, now_ms: float, width: int, height: int) -> bytes:
    header = FrameHeader(seq=seq, t=now_ms, w=width, h=height)
    return encode_frame(header, make_jpeg(seq, width, height))


def schedule(fps: float) -> Iterator[tuple[int, set[str]]]:
    """Yield (tick, kinds of message due) at the frame rate: GPS 1 Hz, compass/IMU 10 Hz."""
    tick = 0
    per_second = max(1, round(fps))
    per_tenth = max(1, round(fps / 10))
    while True:
        due = {"frame"}
        if tick % per_second == 0:
            due |= {"gps", "ping"}
        if tick % per_tenth == 0:
            due |= {"heading", "imu"}
        yield tick, due
        tick += 1


async def run(url: str, seconds: float, fps: float, width: int, height: int) -> None:
    import websockets  # imported here so the pure helpers above have no network dependency

    async with websockets.connect(url, max_size=None) as ws:
        start = time.monotonic()
        await ws.send(hello(time.time() * 1000, width, height, fps))
        clock: dict[str, float | None] = {"rtt_ms": None, "offset_ms": None}

        async def read_replies() -> None:
            async for raw in ws:
                reply = json.loads(raw)
                if reply.get("type") == "pong":
                    received = time.time() * 1000
                    rtt = received - reply["t"]
                    clock["rtt_ms"] = rtt
                    clock["offset_ms"] = reply["server_t"] - (reply["t"] + rtt / 2)
                elif reply.get("type") == "error":
                    log.warning("service rejected a message: %s", reply.get("message"))

        reader = asyncio.create_task(read_replies())
        try:
            for tick, due in schedule(fps):
                elapsed = time.monotonic() - start
                if elapsed >= seconds:
                    break
                now_ms = time.time() * 1000
                pose = pose_at(elapsed)
                if "ping" in due:
                    await ws.send(ping(now_ms, clock["rtt_ms"], clock["offset_ms"]))
                if "gps" in due:
                    await ws.send(gps(now_ms, pose))
                if "heading" in due:
                    await ws.send(heading(now_ms, pose))
                if "imu" in due:
                    await ws.send(imu(now_ms))
                await ws.send(frame(tick, now_ms, width, height))
                next_at = start + (tick + 1) / fps
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))
        finally:
            reader.cancel()
    log.info("sent %d frames", tick)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    args = parser.parse_args()
    if args.fps <= 0 or args.seconds <= 0 or args.width <= 0 or args.height <= 0:
        parser.error("--fps, --seconds, --width and --height must be positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from websockets.exceptions import ConnectionClosed, InvalidURI

    try:
        asyncio.run(run(args.url, args.seconds, args.fps, args.width, args.height))
    except ConnectionClosed as error:
        log.error("the service closed the connection: %s", error)
        raise SystemExit(1) from error
    except (OSError, InvalidURI) as error:
        log.error("could not reach the service at %s: %s", args.url, error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
