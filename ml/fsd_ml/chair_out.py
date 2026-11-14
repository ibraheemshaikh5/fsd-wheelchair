"""Send the model's decisions to the chair through hardware/wheelchair.py.

Only the decision goes: a forward speed and a turn rate, turned into stick
percent for ``Wheelchair.set(x, y)``, which sends it over Tailscale UDP to the
board daemon. Safety (deadman, dropout latch, takeover) lives on the board.

    sender = ChairSender()    # connects to the board and switches FSD on
    sender.send(0.3, 0.1)     # forward 0.3 m/s, turning left 0.1 rad/s
    sender.stop()
    sender.close()            # hands control back to the real stick

A hold thread repeats the last decision every 0.1 s until ``hold_ms`` after it
runs out; then it stops calling ``set`` and the Wheelchair zeroes 0.3 s later.
So if the model stops deciding, the chair stops.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hardware"))
from wheelchair import Wheelchair  # noqa: E402

V_FULL_MPS = 1.5  # ponytail: unmeasured, measure on the ground
W_FULL_RADPS = 1.5  # ponytail: unmeasured, measure on the ground
# Model commands never ask for more than this much stick (before the Wheelchair's deadband).
MAX_PCT = 30
# Long enough to ride over one slow decision, short enough that a stalled model stops the chair.
DEFAULT_HOLD_MS = 600
TICK_S = 0.1


class ChairOutError(Exception):
    """The sender cannot be set up."""


@dataclass(frozen=True, slots=True)
class SendResult:
    sent: bool
    v_mps: float
    w_radps: float
    # Why not, in words, when it did not.
    problem: str | None = None
    # Stick percent handed to Wheelchair.set (x right, y forward).
    x: int = 0
    y: int = 0
    # The board took control back (takeover or dropout): sending must be switched off.
    halted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "v_mps": self.v_mps,
            "w_radps": self.w_radps,
            "problem": self.problem,
            "x": self.x,
            "y": self.y,
            "halted": self.halted,
        }


def _cap(pct: float) -> int:
    return max(-MAX_PCT, min(MAX_PCT, round(pct)))


def to_stick(v_mps: float, w_radps: float) -> tuple[int, int]:
    """Forward speed (never reverse) and turn rate (positive left) as capped stick (x, y)."""
    return _cap(-100 * w_radps / W_FULL_RADPS), _cap(100 * max(0.0, v_mps) / V_FULL_MPS)


class ChairSender:
    """Drives the chair from model decisions. Never raises on a send."""

    def __init__(
        self,
        chair: Wheelchair | None = None,
        hold_ms: int = DEFAULT_HOLD_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if chair is None:
            try:
                chair = Wheelchair()
            except OSError as error:  # no answer, or the board's name does not resolve
                raise ChairOutError(f"cannot reach the chair: {error}") from error
        chair.fsd = True
        self.chair = chair
        self.hold_s = hold_ms / 1000
        self._clock = clock
        self._command: tuple[int, int, float] | None = None  # x, y, when decided
        # Held around every set so a stop can never be followed by a stale repeat.
        self._lock = threading.Lock()
        self._closed = threading.Event()
        threading.Thread(target=self._hold, daemon=True).start()

    def _halt_reason(self) -> str | None:
        stats = self.chair.stats()
        if stats.get("takeover"):
            return "the rider took over with the joystick"
        if stats.get("dropout_latched"):
            return "Wi-Fi dropped out; FSD is off until re-armed"
        return None

    def send(self, v_mps: float, w_radps: float) -> SendResult:
        """Drive at ``v_mps`` forward and ``w_radps`` (positive left), held for ``hold_ms``."""
        v, w = max(0.0, float(v_mps)), float(w_radps)
        return replace(self.send_stick(*to_stick(v, w)), v_mps=v, w_radps=w)

    def send_stick(self, x: float, y: float) -> SendResult:
        """Drive at stick percent (x right, y forward, never reverse), capped, held ``hold_ms``."""
        x, y = _cap(x), _cap(max(0.0, y))
        reason = self._halt_reason()
        if reason is not None:
            self._command = None
            return SendResult(False, 0.0, 0.0, reason, halted=True)
        with self._lock:
            self._command = (x, y, self._clock())
            self.chair.set(x, y)
        return SendResult(True, 0.0, 0.0, x=x, y=y)

    def _hold(self) -> None:
        while not self._closed.wait(TICK_S):
            halted = self._halt_reason() is not None
            with self._lock:
                if self._command is None:
                    continue
                x, y, at = self._command
                if halted or self._clock() - at >= self.hold_s:
                    self._command = None
                else:
                    self.chair.set(x, y)

    def stop(self) -> SendResult:
        """Stop now rather than waiting for the hold to run out."""
        with self._lock:
            self._command = None
            self.chair.stop()
        return SendResult(True, 0.0, 0.0)

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._command = None
        self.chair.close()
