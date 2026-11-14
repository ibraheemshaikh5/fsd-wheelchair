"""The live feed from the driving service's /ws/model: frames placed by capture time."""

from __future__ import annotations

import io
import json
import struct

import numpy as np
from PIL import Image

from fsd_ml.live import LiveFeed, feed_address


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _jpeg(shade: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), (shade, shade, shade)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _frame_message(seq: int, age_ms: float | None, shade: int = 0) -> bytes:
    header = json.dumps({"seq": seq, "t": 1_000.0, "w": 64, "h": 36, "age_ms": age_ms}).encode()
    return struct.pack("<I", len(header)) + header + _jpeg(shade)


def _feed(clock: FakeClock) -> LiveFeed:
    return LiveFeed("http://127.0.0.1:8000", clock=clock)


def test_reads_the_model_feed() -> None:
    assert feed_address("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/ws/model"
    assert feed_address("https://mac.tail1234.ts.net/") == "wss://mac.tail1234.ts.net/ws/model"


def test_a_frame_is_placed_at_its_capture_time() -> None:
    clock = FakeClock(100.0)
    feed = _feed(clock)
    feed._take(json.dumps({"type": "status", "frame": {"seq": 3}}))
    feed._take(_frame_message(3, age_ms=80.0))
    sample = feed.latest()
    assert sample.status["frame"]["seq"] == 3
    assert sample.frame is not None
    assert sample.frame.n == 1
    assert sample.frame.received_s == 100.0
    assert sample.frame.captured_s == 100.0 - 0.080
    assert sample.frame.image.shape == (360, 640, 3)


def test_a_frame_from_before_the_clock_sync_has_no_capture_time() -> None:
    feed = _feed(FakeClock())
    feed._take(_frame_message(0, age_ms=None))
    frame = feed.latest().frame
    assert frame is not None
    assert frame.captured_s is None


def test_a_broken_frame_is_ignored() -> None:
    feed = _feed(FakeClock())
    feed._take(b"\x05\x00\x00\x00{nope" + _jpeg(0))
    feed._take(struct.pack("<I", 999) + b"{}")
    feed._take(_frame_message(0, age_ms=10.0)[:-40])  # cut-off JPEG
    assert feed.latest().frame is None


def test_window_spaces_frames_by_capture_time_not_arrival() -> None:
    clock = FakeClock(100.0)
    feed = _feed(clock)
    # Captured 50 ms apart, but a stall let the first two arrive together with the third.
    for seq, age_ms, shade in ((0, 100.0, 0), (1, 50.0, 120), (2, 0.0, 240)):
        feed._take(_frame_message(seq, age_ms, shade))
    window = feed.window(3, frame_hz=20.0)
    shades = [int(image.mean()) for image in window]
    assert len(window) == 3
    # Oldest first, one per 50 ms step: each frame once, none repeated.
    assert shades[0] < 20 < shades[1] < 200 < shades[2]


def test_window_falls_back_to_arrival_time_before_the_clock_sync() -> None:
    clock = FakeClock(100.0)
    feed = _feed(clock)
    feed._take(_frame_message(0, age_ms=None, shade=0))
    clock.now += 0.05
    feed._take(_frame_message(1, age_ms=None, shade=240))
    shades = [int(image.mean()) for image in feed.window(2, frame_hz=20.0)]
    assert shades[0] < 20 < 200 < shades[1]


def test_history_is_trimmed_by_arrival_time() -> None:
    clock = FakeClock(100.0)
    feed = _feed(clock)
    feed._take(_frame_message(0, age_ms=0.0))
    clock.now += 10.0
    feed._take(_frame_message(1, age_ms=0.0))
    assert len(feed.window(5, frame_hz=20.0)) == 5
    assert {id(image) for image in feed.window(5, frame_hz=20.0)} == {id(feed.latest().frame.image)}


def test_window_is_empty_without_frames() -> None:
    assert _feed(FakeClock()).window(3, frame_hz=20.0) == []


def test_status_messages_keep_the_feed_alive() -> None:
    clock = FakeClock(100.0)
    feed = _feed(clock)
    feed._take(json.dumps({"type": "status"}))
    clock.now += 2.0
    assert feed.latest().silent_s == 2.0
    feed._take("not json")
    assert feed.latest().status == {"type": "status"}


def test_frames_stay_numpy_rgb() -> None:
    feed = _feed(FakeClock())
    feed._take(_frame_message(0, age_ms=5.0, shade=200))
    image = feed.latest().frame.image
    assert isinstance(image, np.ndarray)
    assert image.dtype == np.uint8


def test_window_uses_arrival_times_for_all_when_any_frame_lacks_a_capture_time() -> None:
    clock = FakeClock(100.0)
    feed = _feed(clock)
    # Captured 50 ms apart, then the service lost its clock sync: mixing capture
    # and arrival times would misplace the last frame by the link's latency.
    feed._take(_frame_message(0, age_ms=300.0, shade=0))
    clock.now += 0.05
    feed._take(_frame_message(1, age_ms=300.0, shade=120))
    clock.now += 0.05
    feed._take(_frame_message(2, age_ms=None, shade=240))
    window = feed.window(3, frame_hz=20.0)
    shades = [int(image.mean()) for image in window]
    assert shades[0] < 20 < shades[1] < 200 < shades[2]
    # The newest in the window is the newest that arrived, as latest() reports.
    assert window[-1] is feed.latest().frame.image
