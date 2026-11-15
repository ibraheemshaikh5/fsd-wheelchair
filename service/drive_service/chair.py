"""Live chair metrics from the board daemon (hardware/board/wheelchaird.py writes them ~10x/s).

On the board, they are read from the daemon's file. Anywhere else (e.g. the service on a laptop),
set CHAIR_METRICS_URL to the board's status URL, e.g.
https://wheelchair-unoq.<tailnet>.ts.net:8443/api/status, and a background thread copies the
board's ``chair`` object 5x/s. Speed, turn rate and odometer are ballpark estimates from the stick %
sent to the chair, not measurements. None when neither source has data.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.request
from typing import Any

METRICS_PATH = os.environ.get("CHAIR_METRICS", "/dev/shm/wheelchair.json")
METRICS_URL = os.environ.get("CHAIR_METRICS_URL", "")
FRESH_S = 1.0
POLL_S = 0.2

try:  # python.org builds on macOS ship without CA certificates; the board's tailnet cert needs them
    import certifi

    _TLS = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _TLS = ssl.create_default_context()

_remote: dict[str, Any] = {"chair": None, "at": 0.0}
_poller: threading.Thread | None = None


def _poll(url: str) -> None:
    """Keep _remote updated from the board; never blocks a request."""
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2, context=_TLS) as response:
                chair = json.load(response)["data"]["chair"]
            _remote["chair"], _remote["at"] = chair, time.monotonic()
        except (OSError, ValueError, KeyError, TypeError):
            pass  # board off or unreachable: the copy goes stale and reports fresh=False
        time.sleep(POLL_S)


def _from_board(url: str) -> dict[str, Any] | None:
    global _poller
    if _poller is None:
        _poller = threading.Thread(target=_poll, args=(url,), daemon=True)
        _poller.start()
    chair = _remote["chair"]
    if chair is None:
        return None
    # The board judged its own freshness; also require our copy to be recent.
    fresh = bool(chair.get("fresh")) and time.monotonic() - _remote["at"] < FRESH_S
    return {**chair, "fresh": fresh}


def chair_status() -> dict[str, Any] | None:
    try:
        with open(METRICS_PATH) as f:
            metrics: dict[str, Any] = json.load(f)
    except (OSError, ValueError):
        return _from_board(METRICS_URL) if METRICS_URL else None
    metrics["fresh"] = time.time() - metrics.get("t", 0) < FRESH_S
    return metrics
