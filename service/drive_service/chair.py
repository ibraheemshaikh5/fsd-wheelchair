"""Live chair metrics from the board daemon (hardware/board/wheelchaird.py writes them ~10x/s).

Speed, turn rate and odometer are ballpark estimates from the stick % sent to the chair, not
measurements. None when the daemon isn't running on this machine.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

METRICS_PATH = os.environ.get("CHAIR_METRICS", "/dev/shm/wheelchair.json")
FRESH_S = 1.0


def chair_status() -> dict[str, Any] | None:
    try:
        with open(METRICS_PATH) as f:
            metrics: dict[str, Any] = json.load(f)
    except (OSError, ValueError):
        return None
    metrics["fresh"] = time.time() - metrics.get("t", 0) < FRESH_S
    return metrics
