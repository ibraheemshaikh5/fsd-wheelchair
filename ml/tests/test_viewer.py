from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from fsd_ml.drivers import FanDriver, Goal
from fsd_ml.viewer.server import Session, ViewerError, describe, make_server

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One second of test pattern."""
    path = tmp_path_factory.mktemp("clip") / "clip.mp4"
    pattern = "testsrc=duration=1:size=320x180:rate=20"
    command = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", pattern]
    subprocess.run([*command, "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@pytest.fixture
def client(tmp_path: Path) -> Iterator[httpx.Client]:
    server = make_server(Session(FanDriver(), tmp_path), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=30) as c:
        yield c
    server.shutdown()
    server.server_close()


def test_session_decides_on_a_frame(tmp_path: Path, clip: Path) -> None:
    session = Session(FanDriver(), tmp_path)
    session.open(clip)

    info = session.info()
    assert (info["frames"], info["width"], info["step"]) == (20, 640, 1)

    decision = session.decide(10, (10.0, 2.0), 1.0)
    assert len(decision["candidates"]) == 5
    assert decision["chosen"] == 0
    assert decision["status"] == "clear"
    assert decision["w_radps"] > 0.0  # the goal is to the left
    assert decision["cached"] is False
    assert session.decide(10, (10.0, 2.0), 1.0)["cached"] is True
    with pytest.raises(ViewerError):
        session.decide(99, (10.0, 0.0), 1.0)


def test_page_upload_frames_and_decisions(client: httpx.Client, clip: Path) -> None:
    assert "Model viewer" in client.get("/").text
    assert client.get("/api/info").json()["frames"] == 0

    info = client.post("/api/video", params={"name": "my drive.mp4"}, content=clip.read_bytes())
    assert info.json()["video"] == "my drive.mp4"
    assert info.json()["frames"] == 20

    frame = client.get("/frame/3.jpg")
    assert frame.headers["content-type"] == "image/jpeg"
    assert frame.content[:2] == b"\xff\xd8"
    decided = client.get("/api/decide", params={"i": 3, "gx": 10, "gy": 0, "speed": 1})
    assert decided.json()["v_mps"] == pytest.approx(1.0)

    assert client.get("/frame/999.jpg").status_code == 400
    assert client.get("/api/decide", params={"i": "x"}).status_code == 400


def test_upload_rejects_what_is_not_a_video(client: httpx.Client) -> None:
    wrong_type = client.post("/api/video", params={"name": "notes.txt"}, content=b"hello")
    assert wrong_type.status_code == 400

    broken = client.post("/api/video", params={"name": "broken.mp4"}, content=b"not a video")
    assert broken.status_code == 400
    assert "could not read" in broken.json()["error"]


def test_goal_can_be_an_instruction_or_a_photo(tmp_path: Path, clip: Path) -> None:
    seen = []

    class Recorder(FanDriver):
        def propose(self, frames, goal, motion):
            seen.append(goal)
            return super().propose(frames, Goal(point_xy=(5.0, 0.0)), motion)

    session = Session(Recorder(), tmp_path)
    session.open(clip)

    session.decide(5, (10.0, 0.0), 1.0, "say", " go to the door ")
    assert (seen[-1].instruction, seen[-1].point_xy, seen[-1].image) == (
        "go to the door",
        None,
        None,
    )

    session.decide(5, (10.0, 0.0), 1.0, "photo+point", "", 12)
    assert seen[-1].point_xy == (10.0, 0.0)
    assert seen[-1].image.shape[2] == 3

    # A different instruction is a different decision, not a cached one.
    assert session.decide(5, (10.0, 0.0), 1.0, "say", "turn around")["cached"] is False

    with pytest.raises(ViewerError, match="instruction"):
        session.decide(5, (10.0, 0.0), 1.0, "say", "  ")
    with pytest.raises(ViewerError, match="goal photo"):
        session.decide(5, (10.0, 0.0), 1.0, "photo")
    with pytest.raises(ViewerError, match="goal type"):
        session.decide(5, (10.0, 0.0), 1.0, "dance")


def test_page_is_told_which_model_and_where_it_runs() -> None:
    local = describe(FanDriver())
    assert (local["title"], local["runs_on"]) == ("Fan (a stand-in, not a model)", "this computer")

    class OnCpu(FanDriver):
        name = "flowpilot"
        provider = "CPUExecutionProvider"

    assert describe(OnCpu())["runs_on"] == "this computer (CPU)"
    assert describe(OnCpu())["title"] == "FlowPilot (small)"

    class Hosted(FanDriver):
        name = "remote-omnivla"
        provider = "NVIDIA A10G"
        host = "example.modal.run"

    hosted = describe(Hosted())
    assert (hosted["title"], hosted["remote"]) == ("OmniVLA", True)
    assert hosted["runs_on"] == "example.modal.run (NVIDIA A10G)"


def test_switching_models_keeps_the_video(tmp_path: Path, clip: Path) -> None:
    from fsd_ml import remote

    hosted = remote.make_server(FanDriver(), port=0)
    threading.Thread(target=hosted.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{hosted.server_address[1]}"

    session = Session(FanDriver(), tmp_path, "fan")
    session.open(clip)
    session.decide(5, (10.0, 0.0), 1.0)

    session.switch(f"remote:{address}/")
    assert session.info()["driver"]["title"] == "Fan (a stand-in, not a model)"
    assert session.info()["driver"]["remote"] is True
    assert session.info()["frames"] == 20
    # The other model has not decided on this frame yet.
    assert session.decide(5, (10.0, 0.0), 1.0)["cached"] is False

    models = session.models()
    assert models["current"] == f"remote:{address}"
    assert f"remote:{address}" in [option["id"] for option in models["options"]]
    labels = {option["id"]: option["label"] for option in models["options"]}
    assert labels[f"remote:{address}"] == "Fan (a stand-in, not a model) · 127.0.0.1"
    # The address is remembered for the next run.
    assert Session(FanDriver(), tmp_path).hosted() == [address]

    session.switch("fan")
    assert session.models()["current"] == "fan"
    hosted.shutdown()
    hosted.server_close()


def test_switching_explains_what_went_wrong(tmp_path: Path) -> None:
    session = Session(FanDriver(), tmp_path, "fan")

    with pytest.raises(ViewerError, match="no model called"):
        session.switch("nope")
    with pytest.raises(ViewerError, match="https://"):
        session.switch("remote:http://example.com")
    with pytest.raises(ViewerError, match="could not reach that address"):
        session.switch("remote:http://127.0.0.1:9")
    assert session.models()["current"] == "fan"
    assert session.hosted() == []


def make_clip(path: Path, size: str, rate: int = 30) -> Path:
    pattern = f"testsrc=duration=1:size={size}:rate={rate}"
    command = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", pattern]
    subprocess.run([*command, "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_any_video_becomes_20_fps_16_9_frames(tmp_path: Path) -> None:
    from PIL import Image

    from fsd_ml.video import standardize

    wide = standardize(make_clip(tmp_path / "wide.mp4", "1920x1080"), tmp_path / "wide")
    assert (wide.source_width, wide.source_height, wide.source_fps) == (1920, 1080, 30.0)
    assert wide.frames == 20  # one second at 30 fps becomes 20 frames
    assert (wide.kept, wide.warning) == (1.0, None)
    assert wide.summary() == "1920×1080 at 30 fps → 640×360 at 20 fps"
    with Image.open(tmp_path / "wide" / "000001.jpg") as frame:
        assert frame.size == (640, 360)

    # 4:3 is cut to 16:9, not squashed.
    square = standardize(make_clip(tmp_path / "old.mp4", "640x480"), tmp_path / "old")
    assert square.kept == pytest.approx(0.75)
    assert "middle 75% kept" in square.summary()
    with Image.open(tmp_path / "old" / "000001.jpg") as frame:
        assert frame.size == (640, 360)


def test_portrait_and_slow_videos_are_flagged(tmp_path: Path) -> None:
    from fsd_ml.video import VideoError, standardize

    portrait = standardize(make_clip(tmp_path / "tall.mp4", "360x640"), tmp_path / "tall")
    assert portrait.kept == pytest.approx(0.316, abs=0.01)
    assert "portrait" in portrait.warning

    slow = standardize(make_clip(tmp_path / "slow.mp4", "640x360", rate=10), tmp_path / "slow")
    assert slow.frames == 20
    assert "10 frames a second" in slow.warning

    (tmp_path / "notes.mp4").write_text("not a video")
    with pytest.raises(VideoError, match="not a video"):
        standardize(tmp_path / "notes.mp4", tmp_path / "bad")


def test_live_frames_are_put_into_the_standard_form() -> None:
    import numpy as np

    from fsd_ml.video import standardize_frame

    portrait = np.zeros((640, 360, 3), dtype=np.uint8)
    portrait[300:340] = 255  # a band across the middle survives the cut
    out = standardize_frame(portrait)
    assert out.shape == (360, 640, 3)
    assert out.mean() > 40

    assert standardize_frame(np.zeros((1080, 1920, 3), dtype=np.uint8)).shape == (360, 640, 3)


def test_live_status_becomes_pipeline_input() -> None:
    import numpy as np

    from fsd_ml.live import LiveFrame, LiveSample, feed_address, to_sensors

    assert feed_address("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/ws/viewer"
    assert feed_address("https://mac.tail1234.ts.net/") == "wss://mac.tail1234.ts.net/ws/viewer"
    with pytest.raises(ValueError):
        feed_address("ftp://nope")

    status = {
        "frame": {"age_ms": 60.0, "fresh": True},
        "gps": {"lat": 38.99, "lon": -76.94, "speed_mps": 0.8, "age_ms": 900.0, "fresh": True},
        "heading": {"deg": 90.0, "reference": "true"},
    }
    frame = LiveFrame(7, 0.0, np.zeros((360, 640, 3), dtype=np.uint8))
    sensors = to_sensors(LiveSample(status, frame, 0.1), now_ms=10_000.0)
    assert (sensors.frame_t_ms, sensors.fix_t_ms) == (9_940.0, 9_100.0)
    assert (sensors.lat, sensors.heading_deg, sensors.speed_mps) == (38.99, 90.0, 0.8)

    # A stale fix has no time, and a magnetic heading cannot be used with a map.
    status["gps"]["fresh"] = False
    status["heading"]["reference"] = "magnetic"
    stale = to_sensors(LiveSample(status, frame, 0.1), now_ms=10_000.0)
    assert (stale.fix_t_ms, stale.heading_deg) == (None, None)


def test_live_decide_explains_why_it_is_waiting(tmp_path: Path) -> None:
    session = Session(FanDriver(), tmp_path, "fan")
    with pytest.raises(ViewerError, match="not connected"):
        session.live_decide((10.0, 0.0), 1.0, "point", "")
    with pytest.raises(ViewerError, match="should look like"):
        session.connect_live("ftp://nope")

    session.connect_live("http://127.0.0.1:9")  # nothing listens there
    waiting = session.live_decide((10.0, 0.0), 1.0, "point", "")
    assert "waiting" in waiting
    session.disconnect_live()


def test_a_driving_service_address_in_the_model_box_is_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fsd_ml.viewer import server

    session = Session(FanDriver(), tmp_path, "fan")
    monkeypatch.setattr(server, "is_driving_service", lambda url: True)
    with pytest.raises(server.DrivingServiceGiven) as given:
        session.switch("remote:http://127.0.0.1:9/")
    assert given.value.service == "http://127.0.0.1:9"

    # And the service last used for live is remembered for the next run.
    assert session.last_service() is None
    session.connect_live("http://127.0.0.1:9")
    session.disconnect_live()
    assert Session(FanDriver(), tmp_path).info()["live_service"] == "http://127.0.0.1:9"


def test_ipad_trip_becomes_a_goal_for_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from fsd_ml import live
    from fsd_ml.geo import to_latlon

    origin = (38.99, -76.94)
    chair = to_latlon(0.0, 5.0, origin)  # 5 m up a route that runs north, then east
    here = {"lat": chair[0], "lon": chair[1], "fresh": True}
    north = {"deg": 0.0, "reference": "true"}
    reader = live.ChairGoal("https://chair.example")

    assert reader.read({})[0] is None
    said = {"goal": {"kind": "instruction", "version": 1, "instruction": "go to the kitchen"}}
    goal, note = reader.read(said)
    assert (goal.instruction, goal.point_xy) == ("go to the kitchen", None)
    assert "go to the kitchen" in note

    # A bare destination 20 m due east of a chair facing north is 20 m to its right.
    bare = {"destination": {"distance_m": 20.0, "bearing_deg": 90.0}, "gps": here, "heading": north}
    goal, note = reader.read(bare)
    assert goal.point_xy == pytest.approx((0.0, -20.0), abs=1e-6)
    assert "no route" in note
    # Without knowing which way the chair faces there is nothing to aim at.
    assert reader.read({**bare, "heading": {"deg": 0.0, "reference": "magnetic"}})[0] is None

    points = [to_latlon(e, n, origin) for e, n in ((0, 0), (0, 12), (30, 12))]
    fetched = []

    class Answer:
        def raise_for_status(self):
            return self

        def json(self):
            return {"data": {"route": [{"lat": lat, "lon": lon} for lat, lon in points]}}

    def get(url: str, timeout: float) -> Answer:
        fetched.append(url)
        return Answer()

    monkeypatch.setattr(live.httpx, "get", get)
    routed = {
        "goal": {"kind": "route", "version": 2, "instruction": None, "route_points": 3},
        "destination": {"distance_m": 30.0, "bearing_deg": 80.0},
        "gps": here,
        "heading": north,
    }
    goal, note = reader.read(routed)
    # 10 m further along: 7 m north to the corner, then 3 m east, so ahead and to the right.
    assert goal.point_xy == pytest.approx((7.0, -3.0), abs=0.05)
    assert note == "iPad route: 37 m left"
    reader.read(routed)
    assert fetched == ["https://chair.example/api/goal"]  # the route is fetched once per version
