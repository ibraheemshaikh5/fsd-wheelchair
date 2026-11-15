import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from drive_service import chair


@pytest.fixture(autouse=True)
def _empty_file_cache(monkeypatch):
    monkeypatch.setattr(chair, "_file", {"metrics": None, "at": float("-inf")})


def test_reads_the_daemon_file(tmp_path, monkeypatch):
    path = tmp_path / "wheelchair.json"
    now = time.time()
    path.write_text(json.dumps({"t": now, "speed_mph": 1.5}))
    monkeypatch.setattr(chair, "METRICS_PATH", str(path))
    assert chair.chair_status() == {"t": now, "speed_mph": 1.5, "fresh": True}


def test_no_file_and_no_url_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(chair, "METRICS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(chair, "METRICS_URL", "")
    assert chair.chair_status() is None


def test_copies_the_boards_chair_over_http(tmp_path, monkeypatch):
    body = json.dumps(
        {"success": True, "data": {"chair": {"speed_mph": 2.0, "fresh": True}}}
    ).encode()

    class Board(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(chair, "METRICS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(chair, "METRICS_URL", f"http://127.0.0.1:{server.server_port}/api/status")
    monkeypatch.setattr(chair, "_poller", None)
    monkeypatch.setattr(chair, "_remote", {"chair": None, "at": 0.0})
    deadline = time.monotonic() + 3
    while (status := chair.chair_status()) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    server.shutdown()
    assert status == {"speed_mph": 2.0, "fresh": True}
