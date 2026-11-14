"""The camera sender: ffmpeg command, JPEG splitting, flow control, clock, and a live run."""

import asyncio
import json
import shutil
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from drive_service.app import create_app
from drive_service.camera import CameraSettings
from drive_service.config import Settings
from drive_service.protocol import Hello, parse_frame
from helpers import TINY_JPEG
from scripts import camera_sender
from scripts.camera_sender import (
    EXIT_REFUSED,
    ClockSample,
    InFlight,
    SenderConfig,
    best_sample,
    clock_sample,
    ffmpeg_command,
    frame_message,
    hello_message,
    output_size,
    ping_message,
    split_jpegs,
)

TOKEN = "camera-token-for-tests-0123456789"
START_TIMEOUT_S = 5.0


def _config(**overrides) -> SenderConfig:
    base = {"url": "ws://x/ws/camera", "token": TOKEN, "device": "USB  Live camera"}
    return SenderConfig(**{**base, **overrides})


def test_output_size_keeps_aspect_and_even_height():
    assert output_size(640, 480, 480) == (480, 360)
    assert output_size(1920, 1080, 480) == (480, 270)
    assert output_size(320, 240, 480) == (320, 240)


def test_ffmpeg_command_on_mac_reads_avfoundation_without_audio():
    command = ffmpeg_command(_config(), platform="darwin")
    assert command[0] == "ffmpeg"
    assert "avfoundation" in command
    assert command[command.index("-i") + 1] == "USB  Live camera:none"
    assert "fps=20,scale=480:-2" in command
    assert command[-3:] == ["-f", "image2pipe", "-"]


def test_ffmpeg_command_on_linux_reads_v4l2():
    command = ffmpeg_command(_config(device="/dev/video0"), platform="linux")
    assert "v4l2" in command
    assert command[command.index("-i") + 1] == "/dev/video0"


def test_ffmpeg_command_test_pattern_needs_no_camera():
    command = ffmpeg_command(_config(test_pattern=True), platform="darwin")
    assert "lavfi" in command
    assert "avfoundation" not in command


def test_split_jpegs_across_reads():
    stream = TINY_JPEG + TINY_JPEG + TINY_JPEG[:5]
    frames, rest = split_jpegs(b"junk" + stream)
    assert frames == (TINY_JPEG, TINY_JPEG)
    frames, rest = split_jpegs(rest + TINY_JPEG[5:])
    assert frames == (TINY_JPEG,)
    assert rest == b""


def test_split_jpegs_keeps_half_a_start_marker():
    frames, rest = split_jpegs(TINY_JPEG + b"\xff")
    assert frames == (TINY_JPEG,)
    frames, rest = split_jpegs(rest + TINY_JPEG[1:])
    assert frames == (TINY_JPEG,)


def test_frame_message_is_what_the_service_parses():
    frame = parse_frame(frame_message(7, 1_700_000_000_000.0, 480, 360, TINY_JPEG), 10**6, 1024)
    assert frame.header.seq == 7
    assert (frame.header.w, frame.header.h) == (480, 360)
    assert frame.jpeg == TINY_JPEG


def test_hello_is_valid_for_the_service():
    hello = Hello.model_validate_json(
        hello_message(_config(mount_height_m=0.9, mount_pitch_deg=-5.0), 1_700_000_000_000.0)
    )
    assert hello.camera.label == "USB  Live camera"
    assert (hello.camera.width, hello.camera.height) == (480, 360)
    assert hello.mount.height_m == 0.9


def test_ping_carries_the_best_sample():
    assert "rtt_ms" not in json.loads(ping_message(1.0, None))
    ping = json.loads(ping_message(1.0, ClockSample(rtt_ms=4.0, offset_ms=-2.0)))
    assert (ping["rtt_ms"], ping["offset_ms"]) == (4.0, -2.0)


def test_clock_sample_from_a_pong():
    sample = clock_sample({"t": 1000.0, "server_t": 1510.0}, received_ms=1020.0)
    assert sample == ClockSample(rtt_ms=20.0, offset_ms=500.0)
    assert clock_sample({"t": 1000.0, "server_t": 1.0}, received_ms=900.0) is None
    assert clock_sample({"t": "x"}, received_ms=900.0) is None
    best = best_sample((ClockSample(30.0, 1.0), ClockSample(5.0, 2.0)))
    assert best == ClockSample(5.0, 2.0)
    assert best_sample(()) is None


def test_in_flight_caps_and_forgets_overdue_frames():
    flight = InFlight().sent(1, 0.0).sent(2, 10.0)
    assert not flight.can_send(20.0)
    assert flight.acked(1).can_send(20.0)
    assert flight.acked(2).frames == ()
    assert flight.can_send(1500.0)  # both acks overdue


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[str]:
    settings = Settings(
        record_dir=tmp_path / "recordings",
        web_dist=tmp_path / "missing",
        allowed_hosts=("127.0.0.1", "localhost"),
    )
    camera = CameraSettings(_env_file=None, source="external", token=SecretStr(TOKEN))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(create_app(settings, camera_settings=camera), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + START_TIMEOUT_S
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - only when the server breaks
            raise RuntimeError("test server did not start")
        time.sleep(0.01)
    yield f"127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=START_TIMEOUT_S)
    sock.close()


async def _fake_camera(fps: float = 20.0) -> AsyncIterator[bytes]:
    while True:
        yield TINY_JPEG
        await asyncio.sleep(1.0 / fps)


async def _status_while_sending(base: str, config: SenderConfig, frames) -> dict:
    task = asyncio.create_task(camera_sender.run(config, frames))
    async with httpx.AsyncClient(base_url=f"http://{base}") as http:
        await asyncio.sleep(1.6)
        status = (await http.get("/api/status")).json()["data"]
    assert await task == camera_sender.EXIT_OK
    return status


def test_sender_streams_to_a_live_service(live_server):
    config = _config(url=f"ws://{live_server}/ws/camera", seconds=2.0)
    status = asyncio.run(_status_while_sending(live_server, config, _fake_camera()))
    assert status["frame_source"] == {"kind": "external", "connected": True, "clock_synced": True}
    assert status["frame"]["fresh"] is True
    assert status["stats"]["fps"] > 10
    assert status["camera"]["label"] == "USB  Live camera"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_sender_streams_ffmpeg_test_pattern(live_server):
    config = _config(url=f"ws://{live_server}/ws/camera", seconds=3.0, test_pattern=True)
    frames = camera_sender.ffmpeg_frames(ffmpeg_command(config))
    status = asyncio.run(_status_while_sending(live_server, config, frames))
    assert status["frame"]["fresh"] is True
    assert status["frame"]["w"] == 480


def test_wrong_token_exits_refused(live_server):
    config = _config(url=f"ws://{live_server}/ws/camera", token="wrong-token-0123456789")
    assert asyncio.run(camera_sender.run(config, _fake_camera())) == EXIT_REFUSED
