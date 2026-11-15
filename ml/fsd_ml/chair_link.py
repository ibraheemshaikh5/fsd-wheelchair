"""Send stick commands to the chair through hardware/wheelchair.py.

Everything that moves the chair goes through one ``ChairSender``: a speed and turn rate
become stick percent with ``to_stick``, and ``send_stick`` hands that to
``Wheelchair.set(x, y)``, which sends it over Tailscale UDP to the board daemon. Safety
(deadman, dropout latch, takeover) lives on the board.

    sender = ChairSender()        # connects to the board; one per process, kept for its life
    sender.engage()               # the chair now obeys send_stick, not its own joystick
    sender.send_stick(0, 12)      # 12% forward, held for hold_ms
    sender.brake(0, 0)            # stop now
    sender.release()              # hand control back to the real stick

A hold thread repeats the last command every 0.1 s until ``hold_ms`` after it was given;
then it stops calling ``set`` and the Wheelchair zeroes 0.3 s later. So if whatever is
deciding stops deciding, the chair stops.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hardware"))
from wheelchair import Wheelchair  # noqa: E402

# Nothing sent from here ever asks for more than this much stick (before the Wheelchair
# adds the joystick's dead zone). This is the only place the cap is set.
MAX_PCT = 30
# Long enough to ride over one slow decision, short enough that a stalled model stops the chair.
DEFAULT_HOLD_MS = 600
TICK_S = 0.1
# The reverse pull that brakes the chair on a stop: never harder or longer than this, and
# only when the last forward command was at least MIN_BRAKE_FROM_PCT. Below that the chair
# is all but standing, and a pull back would drive it backward.
MAX_BRAKE_PCT = 25
MAX_BRAKE_MS = 500
BRAKE_TICK_S = 0.05
MIN_BRAKE_FROM_PCT = 5


class ChairOutError(Exception):
    """The sender cannot be set up."""


@dataclass(frozen=True, slots=True)
class SendResult:
    sent: bool
    # Why not, in words, when it did not.
    problem: str | None = None
    # Stick percent handed to Wheelchair.set (x right, y forward).
    x: int = 0
    y: int = 0
    # The board took control back (takeover or dropout): sending must be switched off.
    halted: bool = False


def _cap(pct: float) -> int:
    return max(-MAX_PCT, min(MAX_PCT, round(pct)))


def to_stick(
    v_mps: float, w_radps: float, v_full_mps: float, w_full_radps: float
) -> tuple[int, int]:
    """Forward speed (never reverse) and turn rate (positive left) as capped stick (x, y).

    ``v_full_mps`` and ``w_full_radps`` are what full stick is believed to give (the bench's
    calibration). Over the cap, both shrink together, so the chair drives the same curve
    more slowly rather than a tighter one.
    """
    x = -100 * w_radps / w_full_radps
    y = 100 * max(0.0, v_mps) / v_full_mps
    scale = min(1.0, MAX_PCT / max(abs(x), y, 1e-9))
    return round(x * scale), round(y * scale)


class ChairSender:
    """Drives the chair with stick commands. Never raises on a send."""

    def __init__(
        self,
        chair: Wheelchair | None = None,
        hold_ms: int = DEFAULT_HOLD_MS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if chair is None:
            try:
                chair = Wheelchair()
            except OSError as error:  # no answer, or the board's name does not resolve
                raise ChairOutError(f"cannot reach the chair: {error}") from error
        self.chair = chair
        self.hold_s = hold_ms / 1000
        self._clock = clock
        self._sleep = sleep
        self._command: tuple[int, int, float] | None = None  # x, y, when decided
        # Held around every set so a stop can never be followed by a stale repeat.
        self._lock = threading.Lock()
        self._closed = threading.Event()
        threading.Thread(target=self._hold, daemon=True).start()

    def engage(self) -> None:
        """Have the chair obey this sender. Switching on afresh re-arms it after a dropout."""
        with self._lock:
            self._command = None  # nothing from before the switch is repeated
            self.chair.fsd = True

    def release(self) -> None:
        """Stop, and hand control back to the chair's own joystick."""
        with self._lock:
            self._command = None
            self.chair.stop()
            self.chair.fsd = False

    def _halt_reason(self) -> str | None:
        stats = self.chair.stats()
        if stats.get("takeover"):
            return "the rider took over with the joystick"
        if stats.get("dropout_latched"):
            return "Wi-Fi dropped out; driving is off until switched on again"
        return None

    def send_stick(self, x: float, y: float, reverse: bool = False) -> SendResult:
        """Drive at stick percent (x right, y forward), capped, held ``hold_ms``.

        Never backward unless ``reverse`` is set: a person at the keyboard, or a short
        backing move the pilot asked for.
        """
        x, y = _cap(x), _cap(y if reverse else max(0.0, y))
        reason = self._halt_reason()
        if reason is not None:
            self._command = None
            return SendResult(False, reason, halted=True)
        with self._lock:
            self._command = (x, y, self._clock())
            self.chair.set(x, y)
        return SendResult(True, x=x, y=y)

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

    def brake(self, pct: float, ms: float) -> SendResult:
        """Stop now, pulling the stick back briefly first if the chair was really moving.

        Nothing measures the chair's speed, so "moving" is: a forward command of at least
        ``MIN_BRAKE_FROM_PCT`` was live a moment ago. With ``pct`` or ``ms`` zero it is a
        plain stop.
        """
        pct, ms = min(float(pct), MAX_BRAKE_PCT), min(float(ms), MAX_BRAKE_MS)
        pulled = 0
        with self._lock:
            was, self._command = self._command, None
            moving = (
                was is not None
                and was[1] >= MIN_BRAKE_FROM_PCT
                and self._clock() - was[2] < self.hold_s
            )
            if moving and pct >= 1 and ms > 0 and self._halt_reason() is None:
                pulled = -round(pct)
                for _ in range(max(1, round(ms / 1000 / BRAKE_TICK_S))):
                    self.chair.set(0, pulled)
                    self._sleep(BRAKE_TICK_S)
            self.chair.stop()
        return SendResult(True, y=pulled)

    def telemetry(self) -> dict[str, Any]:
        """What the board last reported: the stick it feeds the chair, the real stick, health."""
        try:
            return dict(self.chair.stats())
        except (TypeError, AttributeError):  # no answer from the board yet
            return {}

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._command = None
        self.chair.close()
