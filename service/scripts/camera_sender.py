"""Send a camera's frames to the driving service's /ws/camera, in place of the phone's.

Usage (from service/, with the service running with DRIVE_CAMERA_SOURCE=external
and the same DRIVE_CAMERA_TOKEN in both shells):

    uv run python -m scripts.camera_sender --device "USB  Live camera"
    uv run python -m scripts.camera_sender --test-pattern     # no camera needed

Today it runs on the Mac with a USB camera. It is self-contained (standard
library, ``websockets`` and the ``ffmpeg`` program) so the same file can run on
the chair's UNO Q later, with the camera plugged in there (``--device
/dev/video0``) and ``--url`` pointing at the Mac over Tailscale.

ffmpeg reads the camera, drops to ``--fps`` and ``--width`` (20 fps, 480 px: what
the driving model uses) and writes JPEGs to a pipe. Each one is stamped with the
wall clock as it comes out of the pipe, so its age includes ffmpeg's delay but
not the capture itself (about one frame). The service ages frames by this
sender's own clock, synced with the phone protocol's ping/pong, so the service and
the sender may run on different machines.

Latest wins, as on the phone: a frame is sent only while fewer than
``MAX_FRAMES_IN_FLIGHT`` are waiting for an ack; otherwise it is skipped, never
queued. The token is read from an environment variable, never from the command
line (other users can read process arguments). Exits 3 if the service refuses
the token, 5 if ffmpeg stops (for example, no camera access), and otherwise
reconnects with backoff.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import struct
import sys
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace

log = logging.getLogger("camera_sender")

DEFAULT_URL = "ws://127.0.0.1:8000/ws/camera"
DEFAULT_TOKEN_ENV = "DRIVE_CAMERA_TOKEN"
DEFAULT_MAC_DEVICE = "USB  Live camera"
DEFAULT_LINUX_DEVICE = "/dev/video0"
PROTOCOL_VERSION = 1
MAX_FRAMES_IN_FLIGHT = 2
ACK_TIMEOUT_MS = 1000.0
PING_INTERVAL_S = 1.0
CLOCK_WINDOW = 20
BACKOFF_START_S = 0.5
BACKOFF_MAX_S = 10.0
BACKOFF_RESET_MS = 2000.0
READ_CHUNK = 65_536
# ffmpeg's -q:v for MJPEG: 2 is best, 31 worst. 5 is close to the phone's 0.7.
DEFAULT_QUALITY = 5
EXIT_OK = 0
EXIT_REFUSED = 3
EXIT_CAMERA = 5

_LENGTH_PREFIX = struct.Struct("<I")
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


@dataclass(frozen=True, slots=True)
class SenderConfig:
    url: str
    token: str
    device: str
    input_width: int = 640
    input_height: int = 480
    input_fps: int = 30
    fps: int = 20
    width: int = 480
    quality: int = DEFAULT_QUALITY
    test_pattern: bool = False
    mount_height_m: float | None = None
    mount_pitch_deg: float | None = None
    seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ClockSample:
    rtt_ms: float
    offset_ms: float


@dataclass(frozen=True, slots=True)
class InFlight:
    """Frames sent and not yet acked, as (seq, monotonic ms sent)."""

    frames: tuple[tuple[int, float], ...] = ()

    def live(self, now_ms: float) -> InFlight:
        """Without frames whose ack is overdue, so a lost ack never stalls the stream."""
        return InFlight(tuple(f for f in self.frames if now_ms - f[1] < ACK_TIMEOUT_MS))

    def can_send(self, now_ms: float) -> bool:
        return len(self.live(now_ms).frames) < MAX_FRAMES_IN_FLIGHT

    def sent(self, seq: int, now_ms: float) -> InFlight:
        return InFlight((*self.live(now_ms).frames, (seq, now_ms)))

    def acked(self, seq: int) -> InFlight:
        """An ack covers its seq and every earlier one."""
        return InFlight(tuple(f for f in self.frames if f[0] > seq))


def wall_ms() -> float:
    return time.time() * 1000.0


def mono_ms() -> float:
    return time.monotonic() * 1000.0


def output_size(input_width: int, input_height: int, width: int) -> tuple[int, int]:
    """The frame size after ffmpeg's ``scale=W:-2`` (height kept even)."""
    out_width = min(width, input_width)
    height = round(out_width * input_height / input_width / 2) * 2
    return out_width, height


def _input_args(config: SenderConfig, platform: str) -> list[str]:
    size = f"{config.input_width}x{config.input_height}"
    if config.test_pattern:
        return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={config.input_fps}"]
    rate = str(config.input_fps)
    if platform == "darwin":
        # This camera offers raw uyvy422; "none" opens no audio device.
        return [
            "-f", "avfoundation", "-pixel_format", "uyvy422",
            "-video_size", size, "-framerate", rate, "-i", f"{config.device}:none",
        ]  # fmt: skip
    return [
        "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", size, "-framerate", rate, "-i", config.device,
    ]  # fmt: skip


def ffmpeg_command(config: SenderConfig, platform: str = sys.platform) -> list[str]:
    """The ffmpeg command that writes JPEG frames to stdout."""
    width, _ = output_size(config.input_width, config.input_height, config.width)
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-fflags", "nobuffer", "-flags", "low_delay",
        *_input_args(config, platform),
        "-an", "-vf", f"fps={config.fps},scale={width}:-2",
        "-c:v", "mjpeg", "-q:v", str(config.quality), "-f", "image2pipe", "-",
    ]  # fmt: skip


def split_jpegs(buffer: bytes) -> tuple[tuple[bytes, ...], bytes]:
    """Whole JPEGs found in ``buffer``, and the bytes left over for the next read.

    ffmpeg's MJPEG encoder writes plain JPEGs with no embedded thumbnail, so the
    first end marker after a start marker ends the image.
    """
    frames: list[bytes] = []
    rest = buffer
    while True:
        start = rest.find(_SOI)
        if start < 0:
            # Keep a trailing 0xFF: it may be the first half of the next start marker.
            return tuple(frames), rest[-1:] if rest.endswith(b"\xff") else b""
        end = rest.find(_EOI, start + len(_SOI))
        if end < 0:
            return tuple(frames), rest[start:]
        frames.append(rest[start : end + len(_EOI)])
        rest = rest[end + len(_EOI) :]


def frame_message(seq: int, t_ms: float, width: int, height: int, jpeg: bytes) -> bytes:
    """``[uint32 LE header length][header JSON][JPEG]``, as the phone sends."""
    header = json.dumps({"seq": seq, "t": t_ms, "w": width, "h": height}).encode()
    return _LENGTH_PREFIX.pack(len(header)) + header + jpeg


def hello_message(config: SenderConfig, t_ms: float) -> str:
    width, height = output_size(config.input_width, config.input_height, config.width)
    label = "test pattern" if config.test_pattern else config.device.strip()
    message: dict[str, object] = {
        "v": PROTOCOL_VERSION,
        "type": "hello",
        "t": t_ms,
        "user_agent": f"camera_sender/1 ({sys.platform})",
        "camera": {"label": label, "width": width, "height": height, "fps": float(config.fps)},
    }
    if config.mount_height_m is not None and config.mount_pitch_deg is not None:
        message["mount"] = {"height_m": config.mount_height_m, "pitch_deg": config.mount_pitch_deg}
    return json.dumps(message)


def ping_message(t_ms: float, best: ClockSample | None) -> str:
    message: dict[str, object] = {"v": PROTOCOL_VERSION, "type": "ping", "t": t_ms}
    if best is not None:
        message["rtt_ms"] = best.rtt_ms
        message["offset_ms"] = best.offset_ms
    return json.dumps(message)


def clock_sample(pong: dict[str, object], received_ms: float) -> ClockSample | None:
    """Round trip and offset (``server = sender + offset``) from one pong."""
    sent, server = pong.get("t"), pong.get("server_t")
    if not isinstance(sent, int | float) or not isinstance(server, int | float):
        return None
    rtt = received_ms - sent
    if rtt < 0:
        return None
    return ClockSample(rtt_ms=rtt, offset_ms=server - (sent + rtt / 2))


def best_sample(samples: tuple[ClockSample, ...]) -> ClockSample | None:
    """The sample with the shortest round trip, the least distorted by delay."""
    return min(samples, key=lambda s: s.rtt_ms) if samples else None


class _Latest:
    """The newest frame from the camera, handed from the reader to the sender."""

    def __init__(self) -> None:
        self.frame: tuple[int, float, bytes] | None = None
        self.produced = 0
        self.changed = asyncio.Event()
        self.ended = False

    def put(self, t_ms: float, jpeg: bytes) -> None:
        self.frame = (self.produced, t_ms, jpeg)
        self.produced += 1
        self.changed.set()

    def end(self) -> None:
        self.ended = True
        self.changed.set()


async def ffmpeg_frames(command: Sequence[str]) -> AsyncIterator[bytes]:
    """JPEGs from an ffmpeg process, as fast as it writes them."""
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    assert process.stdout is not None and process.stderr is not None
    buffer = b""
    try:
        while chunk := await process.stdout.read(READ_CHUNK):
            frames, buffer = split_jpegs(buffer + chunk)
            for frame in frames:
                yield frame
        error = (await process.stderr.read()).decode(errors="replace").strip()
        log.error("ffmpeg stopped (exit %s): %s", await process.wait(), error or "no message")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _read_camera(frames: AsyncIterator[bytes], latest: _Latest) -> None:
    try:
        async for jpeg in frames:
            latest.put(wall_ms(), jpeg)
    finally:
        latest.end()


@dataclass(frozen=True, slots=True)
class _Link:
    """Per-connection state: frames in flight and clock samples."""

    in_flight: InFlight = InFlight()
    samples: tuple[ClockSample, ...] = ()


def _apply_reply(link: _Link, text: str) -> _Link:
    message = json.loads(text)
    kind = message.get("type")
    if kind == "ack" and isinstance(message.get("seq"), int):
        return replace(link, in_flight=link.in_flight.acked(message["seq"]))
    if kind == "pong":
        sample = clock_sample(message, wall_ms())
        if sample is not None:
            return replace(link, samples=(*link.samples, sample)[-CLOCK_WINDOW:])
    elif kind == "error":
        log.warning("service refused a message: %s", message.get("message"))
    return link


async def _receive(ws, state: dict[str, _Link]) -> None:
    async for text in ws:
        if isinstance(text, str):
            state["link"] = _apply_reply(state["link"], text)


async def _ping(ws, state: dict[str, _Link]) -> None:
    while True:
        await ws.send(ping_message(wall_ms(), best_sample(state["link"].samples)))
        await asyncio.sleep(PING_INTERVAL_S)


async def _send_frames(ws, config: SenderConfig, latest: _Latest, state: dict[str, _Link]) -> None:
    width, height = output_size(config.input_width, config.input_height, config.width)
    last_seq = -1
    while not latest.ended:
        await latest.changed.wait()
        latest.changed.clear()
        frame = latest.frame
        if frame is None or frame[0] == last_seq:
            continue
        seq, t_ms, jpeg = frame
        last_seq = seq
        now = mono_ms()
        if not state["link"].in_flight.can_send(now):
            continue  # skipped: the next frame is newer anyway
        state["link"] = replace(state["link"], in_flight=state["link"].in_flight.sent(seq, now))
        await ws.send(frame_message(seq, t_ms, width, height, jpeg))


async def _session(ws, config: SenderConfig, latest: _Latest) -> None:
    state = {"link": _Link()}
    await ws.send(hello_message(config, wall_ms()))
    tasks = [
        asyncio.create_task(_receive(ws, state)),
        asyncio.create_task(_ping(ws, state)),
        asyncio.create_task(_send_frames(ws, config, latest, state)),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _connect_loop(config: SenderConfig, latest: _Latest) -> int:
    import websockets
    from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

    headers = {"Authorization": f"Bearer {config.token}"}
    backoff = BACKOFF_START_S
    while not latest.ended:
        opened = mono_ms()
        try:
            async with websockets.connect(
                config.url, additional_headers=headers, max_queue=16
            ) as ws:
                log.info("connected to %s", config.url)
                await _session(ws, config, latest)
        except InvalidStatus as error:
            log.error(
                "the service refused the camera (HTTP %d): check DRIVE_CAMERA_TOKEN and that "
                "the service runs with DRIVE_CAMERA_SOURCE=external",
                error.response.status_code,
            )
            return EXIT_REFUSED
        except (OSError, InvalidHandshake, ConnectionClosed) as error:
            log.warning("camera link down: %s", error)
        if mono_ms() - opened > BACKOFF_RESET_MS:  # it worked for a while: retry quickly
            backoff = BACKOFF_START_S
        if latest.ended:
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX_S)
    return EXIT_CAMERA


async def run(config: SenderConfig, frames: AsyncIterator[bytes]) -> int:
    """Stream ``frames`` until they end, ``config.seconds`` pass or the token is refused."""
    latest = _Latest()
    reader = asyncio.create_task(_read_camera(frames, latest))
    try:
        if config.seconds is None:
            return await _connect_loop(config, latest)
        try:
            return await asyncio.wait_for(_connect_loop(config, latest), config.seconds)
        except TimeoutError:
            return EXIT_OK
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


def _default_device() -> str:
    return DEFAULT_MAC_DEVICE if sys.platform == "darwin" else DEFAULT_LINUX_DEVICE


def _parse_args(argv: Sequence[str] | None) -> SenderConfig:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--device", default=_default_device(), help="camera name or index")
    parser.add_argument("--input-size", default="640x480", help="camera mode, WxH")
    parser.add_argument("--input-fps", type=int, default=30)
    parser.add_argument("--fps", type=int, default=20, help="frames sent per second")
    parser.add_argument("--width", type=int, default=480, help="long side sent, px")
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="2 best .. 31")
    parser.add_argument("--test-pattern", action="store_true", help="ffmpeg test video")
    parser.add_argument("--mount-height-m", type=float, default=None)
    parser.add_argument("--mount-pitch-deg", type=float, default=None)
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long")
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        parser.error(f"set the camera token in the {args.token_env} environment variable")
    try:
        in_w, in_h = (int(part) for part in args.input_size.lower().split("x"))
    except ValueError:
        parser.error("--input-size must look like 640x480")
    if min(in_w, in_h, args.input_fps, args.fps, args.width) <= 0:
        parser.error("sizes and rates must be positive")
    if not 2 <= args.quality <= 31:
        parser.error("--quality must be 2 to 31")
    if (args.mount_height_m is None) != (args.mount_pitch_deg is None):
        parser.error("give both --mount-height-m and --mount-pitch-deg, or neither")
    if args.seconds is not None and args.seconds <= 0:
        parser.error("--seconds must be positive")
    return SenderConfig(
        url=args.url,
        token=token,
        device=args.device,
        input_width=in_w,
        input_height=in_h,
        input_fps=args.input_fps,
        fps=min(args.fps, args.input_fps),
        width=args.width,
        quality=args.quality,
        test_pattern=args.test_pattern,
        mount_height_m=args.mount_height_m,
        mount_pitch_deg=args.mount_pitch_deg,
        seconds=args.seconds,
    )


def main(argv: Sequence[str] | None = None) -> None:
    config = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    command = ffmpeg_command(config)
    log.info("camera: %s", " ".join(command))
    raise SystemExit(asyncio.run(run(config, ffmpeg_frames(command))))


if __name__ == "__main__":
    main()
