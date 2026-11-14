import json
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from drive_service.app import create_app
from drive_service.config import Settings
from helpers import TINY_JPEG, as_json, frame_bytes, gps_dict, hello_dict, ping_dict

HOST = "testserver"
TEST_HOSTS = (HOST,)
SAME_ORIGIN = {"origin": f"http://{HOST}"}
OTHER_ORIGIN = {"origin": "https://evil.example"}
NO_DISK_IS_THIS_BIG = 10**18
IDLE_TIMEOUT_S = 0.2
SLOW_VIEWER_FPS = 20.0
TIMING_SLACK_S = 0.01


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        record_dir=tmp_path / "recordings",
        web_dist=tmp_path / "missing",
        max_consecutive_errors=3,
        max_viewers=1,
        viewer_fps=50,
        allowed_hosts=TEST_HOSTS,
        recorder_min_free_bytes=0,
    )


@pytest.fixture
def client(settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _status(client: TestClient) -> dict:
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["error"] is None
    return body["data"]


def test_status_before_any_phone_connects(client):
    status = _status(client)
    assert status["phone_connected"] is False
    assert status["frame"] is None


def test_phone_stream_reaches_status(client):
    with client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone:
        phone.send_text(as_json(hello_dict()))
        phone.send_text(as_json(gps_dict(lat=38.5)))
        phone.send_bytes(frame_bytes(seq=4))
        phone.send_text(as_json(ping_dict(t=77.0)))
        pong = json.loads(phone.receive_text())

        assert pong["type"] == "pong"
        assert pong["t"] == 77.0
        status = _status(client)
        assert status["phone_connected"] is True
        assert status["gps"]["lat"] == 38.5
        assert status["frame"]["seq"] == 4
        assert status["camera"]["label"] == "Back Ultra Wide Camera"

    assert _status(client)["phone_connected"] is False


def test_invalid_message_gets_an_error_reply_and_is_counted(client):
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_text(as_json(gps_dict(lat=500)))
        reply = json.loads(phone.receive_text())
        assert reply["type"] == "error"
        assert "lat" in reply["message"]
        assert _status(client)["stats"]["rejected"] == 1


def test_invalid_frame_gets_an_error_reply(client):
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_bytes(b"\x00")
        assert json.loads(phone.receive_text())["type"] == "error"


def test_repeated_garbage_closes_the_connection(client):
    with client.websocket_connect("/ws/phone") as phone:
        for _ in range(3):
            phone.send_text("garbage")
            phone.receive_text()
        with pytest.raises(WebSocketDisconnect) as closed:
            phone.receive_text()
        assert closed.value.code == 1008


def test_a_good_message_resets_the_error_count(client):
    with client.websocket_connect("/ws/phone") as phone:
        for _ in range(4):
            phone.send_text("garbage")
            phone.receive_text()
            phone.send_text(as_json(ping_dict()))
            assert json.loads(phone.receive_text())["type"] == "pong"


def _pong(phone: WebSocketTestSession) -> None:
    """Round-trip a ping so everything sent before it has been processed."""
    phone.send_text(as_json(ping_dict()))
    assert json.loads(phone.receive_text())["type"] == "pong"


def test_newer_phone_replaces_older(client):
    with ExitStack() as first_stack:
        first = first_stack.enter_context(client.websocket_connect("/ws/phone"))
        _pong(first)
        with client.websocket_connect("/ws/phone") as second:
            with pytest.raises(WebSocketDisconnect) as closed:
                first.receive_text()
            assert closed.value.code == 4410
            first_stack.close()

            second.send_text(as_json(gps_dict(lat=12.5)))
            _pong(second)
            status = _status(client)
            assert status["phone_connected"] is True
            assert status["gps"]["lat"] == 12.5
        assert _status(client)["phone_connected"] is False


def test_replaced_phone_messages_are_ignored(client):
    with client.websocket_connect("/ws/phone") as first:
        _pong(first)
        with client.websocket_connect("/ws/phone") as second:
            _pong(second)
            first.send_text(as_json(gps_dict(lat=-1.0)))
            with pytest.raises(WebSocketDisconnect):
                first.receive_text()
            _pong(second)
            assert _status(client)["gps"] is None


def test_silent_phone_is_dropped(tmp_path):
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        allowed_hosts=TEST_HOSTS,
        phone_idle_timeout_s=IDLE_TIMEOUT_S,
    )
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect("/ws/phone") as phone:
            started = time.monotonic()
            with pytest.raises(WebSocketDisconnect) as closed:
                phone.receive_text()
            assert closed.value.code == 4408
            assert time.monotonic() - started < 1.0
        assert _status(client)["phone_connected"] is False
        with client.websocket_connect("/ws/phone") as again:
            _pong(again)
            assert _status(client)["phone_connected"] is True


def test_idle_timeout_default_is_five_seconds(tmp_path):
    assert Settings(record_dir=tmp_path).phone_idle_timeout_s == 5.0


def test_cross_origin_phone_socket_is_refused(client):
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/ws/phone", headers=OTHER_ORIGIN),
    ):
        pass
    assert closed.value.code == 1008


def test_cross_origin_viewer_socket_is_refused(client):
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/viewer", headers=OTHER_ORIGIN),
    ):
        pass


def test_configured_origin_is_allowed(tmp_path):
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        allowed_origins=("https://chair.example.ts.net",),
        allowed_hosts=TEST_HOSTS,
    )
    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/ws/phone", headers={"origin": "https://chair.example.ts.net"}),
    ):
        assert _status(client)["phone_connected"] is True


def test_forwarded_host_counts_as_same_origin(client):
    headers = {"origin": "https://chair.example.ts.net", "x-forwarded-host": "chair.example.ts.net"}
    with client.websocket_connect("/ws/phone", headers=headers):
        assert _status(client)["phone_connected"] is True


def test_viewer_receives_status_then_latest_frame(client):
    with client.websocket_connect("/ws/phone") as phone:
        phone.send_bytes(frame_bytes(seq=1))
        phone.send_text(as_json(ping_dict()))
        phone.receive_text()  # the frame has been processed once the pong arrives

        with client.websocket_connect("/ws/viewer", headers=SAME_ORIGIN) as viewer:
            status = json.loads(viewer.receive_text())
            assert status["type"] == "status"
            assert status["frame"]["seq"] == 1
            assert viewer.receive_bytes() == TINY_JPEG
            # The same frame is not sent twice.
            assert json.loads(viewer.receive_text())["type"] == "status"
            assert json.loads(viewer.receive_text())["type"] == "status"


def test_viewer_messages_do_not_speed_up_status(tmp_path):
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        allowed_hosts=TEST_HOSTS,
        viewer_fps=SLOW_VIEWER_FPS,
    )
    with TestClient(create_app(settings)) as client, client.websocket_connect("/ws/viewer") as v:
        v.receive_text()
        started = time.monotonic()
        for _ in range(200):
            v.send_text("hurry up")
        for _ in range(4):
            assert json.loads(v.receive_text())["type"] == "status"
        elapsed = time.monotonic() - started
    assert elapsed >= 3 / SLOW_VIEWER_FPS - TIMING_SLACK_S


def test_viewer_limit(client):
    with client.websocket_connect("/ws/viewer") as first:
        first.receive_text()
        with client.websocket_connect("/ws/viewer") as second:
            with pytest.raises(WebSocketDisconnect) as closed:
                second.receive_text()
            assert closed.value.code == 4429


@contextmanager
def _phone_said_hello(client: TestClient) -> Iterator[WebSocketTestSession]:
    """A connected phone whose hello the service has already processed."""
    with client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone:
        phone.send_text(as_json(hello_dict()))
        phone.send_text(as_json(ping_dict()))
        phone.receive_text()  # the hello has been processed once the pong arrives
        yield phone


def test_recording_round_trip(client, settings):
    with _phone_said_hello(client) as phone:
        start = client.post("/api/recording/start", headers=SAME_ORIGIN)
        assert start.status_code == 200
        session = start.json()["data"]["session"]
        assert start.json()["data"]["active"] is True

        phone.send_text(as_json(gps_dict()))
        phone.send_bytes(frame_bytes(seq=0))
        phone.send_text(as_json(ping_dict()))
        phone.receive_text()

    stop = client.post("/api/recording/stop", headers=SAME_ORIGIN)
    assert stop.status_code == 200
    assert stop.json()["data"] == {
        "active": False,
        "session": session,
        "frames": 1,
        "sensors": 1,
        "dropped": 0,
        "error": None,
        "finishing": False,
        "end_reason": None,
    }
    folder = settings.record_dir / session
    assert (folder / "frames" / "000001.jpg").read_bytes() == TINY_JPEG
    assert len((folder / "sensors.jsonl").read_text().splitlines()) == 1
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["camera"]["label"] == "Back Ultra Wide Camera"
    assert meta["end_reason"] is None


def test_starting_twice_is_a_conflict(client):
    with _phone_said_hello(client):
        assert client.post("/api/recording/start").status_code == 200
        second = client.post("/api/recording/start")
        assert second.status_code == 409
        assert second.json() == {
            "success": False,
            "data": None,
            "error": "a recording is already running",
        }
        client.post("/api/recording/stop")


def test_recording_start_refused_without_phone(client):
    response = client.post("/api/recording/start", headers=SAME_ORIGIN)
    assert response.status_code == 409
    assert response.json() == {
        "success": False,
        "data": None,
        "error": "phone is not connected or has not sent hello yet",
    }
    assert _status(client)["recording"]["active"] is False


def test_recording_start_refused_before_hello(client):
    with client.websocket_connect("/ws/phone", headers=SAME_ORIGIN) as phone:
        phone.send_text(as_json(ping_dict()))
        phone.receive_text()
        response = client.post("/api/recording/start", headers=SAME_ORIGIN)
    assert response.status_code == 409
    assert response.json()["success"] is False
    assert "hello" in response.json()["error"]


def test_recording_start_refused_when_disk_is_nearly_full(tmp_path):
    settings = Settings(
        record_dir=tmp_path / "recordings",
        web_dist=tmp_path / "missing",
        allowed_hosts=TEST_HOSTS,
        recorder_min_free_bytes=NO_DISK_IS_THIS_BIG,
    )
    with TestClient(create_app(settings)) as client, _phone_said_hello(client):
        response = client.post("/api/recording/start", headers=SAME_ORIGIN)
        assert _status(client)["recording"]["active"] is False
    assert response.status_code == 507
    assert response.json()["success"] is False
    assert "free disk" in response.json()["error"]


def test_cross_origin_post_is_forbidden(client):
    with _phone_said_hello(client):
        response = client.post("/api/recording/start", headers=OTHER_ORIGIN)
        assert response.status_code == 403
        assert response.json()["success"] is False
        assert _status(client)["recording"]["active"] is False


def test_destination_round_trip(client):
    assert _status(client)["destination"] is None

    response = client.post(
        "/api/destination", json={"lat": 38.99, "lon": -76.94}, headers=SAME_ORIGIN
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {"lat": 38.99, "lon": -76.94, "distance_m": None, "bearing_deg": None},
        "error": None,
    }
    # No phone, so no fresh fix to measure from.
    assert _status(client)["destination"]["distance_m"] is None

    cleared = client.delete("/api/destination", headers=SAME_ORIGIN)
    assert cleared.status_code == 200
    assert cleared.json()["success"] is True
    assert _status(client)["destination"] is None


def test_destination_reaches_the_viewer(client):
    client.post("/api/destination", json={"lat": 38.99, "lon": -76.94})
    with client.websocket_connect("/ws/viewer") as viewer:
        status = json.loads(viewer.receive_text())
    assert status["destination"]["lat"] == 38.99


@pytest.mark.parametrize(
    "body",
    [
        {"lat": 91, "lon": 0},
        {"lat": 0, "lon": "east"},
        {"lat": 0},
        {"lat": 0, "lon": 0, "note": "x"},
    ],
)
def test_bad_destination_is_refused(client, body):
    response = client.post("/api/destination", json=body)
    assert response.status_code == 422
    assert response.json()["success"] is False
    assert response.json()["error"].startswith("destination must be")
    assert _status(client)["destination"] is None


def test_destination_body_must_be_json(client):
    response = client.post(
        "/api/destination", content=b"lat=1", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == 'body must be JSON like {"lat": 38.99, "lon": -76.94}'


def test_oversized_destination_body_is_refused(client):
    response = client.post("/api/destination", content=b"{" + b" " * 5000 + b"}")
    assert response.status_code == 413
    assert _status(client)["destination"] is None


def test_cross_origin_destination_changes_are_forbidden(client):
    client.post("/api/destination", json={"lat": 1.0, "lon": 2.0})
    posted = client.post("/api/destination", json={"lat": 3.0, "lon": 4.0}, headers=OTHER_ORIGIN)
    deleted = client.delete("/api/destination", headers=OTHER_ORIGIN)
    assert posted.status_code == 403
    assert deleted.status_code == 403
    assert _status(client)["destination"]["lat"] == 1.0


def test_destination_changes_count_against_the_control_limit(tmp_path):
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        max_control_rate_hz=2,
        allowed_hosts=TEST_HOSTS,
    )
    with TestClient(create_app(settings)) as client:
        codes = [client.delete("/api/destination").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_api_rate_limit(tmp_path):
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        max_api_rate_hz=3,
        allowed_hosts=TEST_HOSTS,
    )
    with TestClient(create_app(settings)) as client:
        codes = [client.get("/api/status").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_status_flood_does_not_block_recording_stop(tmp_path):
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        max_api_rate_hz=2,
        allowed_hosts=TEST_HOSTS,
    )
    with TestClient(create_app(settings)) as client:
        codes = [client.get("/api/status").status_code for _ in range(3)]
        stop = client.post("/api/recording/stop", headers=SAME_ORIGIN)
    assert codes == [200, 200, 429]
    assert stop.status_code == 200
    assert stop.json()["success"] is True


def test_control_requests_have_their_own_limit(tmp_path):
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        max_control_rate_hz=2,
        allowed_hosts=TEST_HOSTS,
    )
    with TestClient(create_app(settings)) as client:
        codes = [client.post("/api/recording/stop").status_code for _ in range(3)]
        status = client.get("/api/status")
    assert codes == [200, 200, 429]
    assert status.status_code == 200


def test_foreign_site_cannot_use_up_the_control_budget(tmp_path):
    # A page on another site can fire no-cors POSTs; each is refused, and none may
    # spend the budget a genuine Stop recording needs.
    settings = Settings(
        record_dir=tmp_path,
        web_dist=tmp_path / "missing",
        max_control_rate_hz=2,
        allowed_hosts=TEST_HOSTS,
    )
    with TestClient(create_app(settings)) as client:
        foreign = [
            client.post("/api/recording/stop", headers=OTHER_ORIGIN).status_code for _ in range(10)
        ]
        destination = client.delete("/api/destination", headers=OTHER_ORIGIN)
        stop = client.post("/api/recording/stop", headers=SAME_ORIGIN)
    assert foreign == [403] * 10
    assert destination.status_code == 403
    assert destination.json()["success"] is False
    assert stop.status_code == 200


def test_control_rate_default_is_five_per_second(tmp_path):
    assert Settings(record_dir=tmp_path).max_control_rate_hz == 5


def test_recording_stops_on_shutdown(settings):
    with TestClient(create_app(settings)) as client:
        with _phone_said_hello(client):
            session = client.post("/api/recording/start").json()["data"]["session"]
        assert _status(client)["recording"]["active"] is True
    meta = json.loads((settings.record_dir / session / "meta.json").read_text())
    assert "ended_utc" in meta


def test_built_web_pages_are_served(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>chair</h1>")
    settings = Settings(record_dir=tmp_path / "rec", web_dist=dist, allowed_hosts=TEST_HOSTS)
    with TestClient(create_app(settings)) as client:
        assert "chair" in client.get("/").text
        assert _status(client)["phone_connected"] is False
