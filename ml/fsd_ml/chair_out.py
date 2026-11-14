"""Send the model's decisions to the chair, by way of the driving service.

Only the decision goes: a forward speed and a turn rate. The model's paths,
scores and frames stay here.

The service holds the link to the Arduino on the chair (``/ws/chair``) and sends
it a drive command 20 times a second. This posts each decision to the service's
``POST /api/chair/drive``; the service passes it on until ``hold_ms`` runs out
or a newer one arrives. If this stops posting, the chair stops.

    sender = ChairSender("http://127.0.0.1:8000", token)
    sender.send(0.3, 0.1)     # forward 0.3 m/s, turning left 0.1 rad/s
    sender.stop()

What the Arduino receives is shaped by the service (docs/chair-link.md), so the
board's message format can change without touching this.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

TOKEN_ENV = "DRIVE_CHAIR_DRIVER_TOKEN"
# Long enough to ride over one slow decision, short enough that a stalled model stops the chair.
DEFAULT_HOLD_MS = 600
TIMEOUT_S = 0.5
# The service passes commands on 20 times a second; sending faster only spends its rate budget.
MIN_INTERVAL_S = 0.05


class ChairOutError(Exception):
    """The sender cannot be set up."""


@dataclass(frozen=True, slots=True)
class SendResult:
    # Whether the service took the command.
    sent: bool
    # What the service is passing on to the chair, which may be lower than was asked.
    v_mps: float
    w_radps: float
    # Why not, in words, when it did not.
    problem: str | None = None
    # The chair link as the service reported it (GET /api/chair's data).
    chair: dict[str, Any] | None = None
    # The service lowered the command to the chair's caps.
    limited: bool = False

    def as_dict(self) -> dict[str, Any]:
        connected = bool(self.chair and self.chair.get("connected"))
        return {
            "sent": self.sent,
            "v_mps": self.v_mps,
            "w_radps": self.w_radps,
            "problem": self.problem,
            "limited": self.limited,
            "chair_connected": connected,
        }


def token_from_env() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ChairOutError(
            f"set {TOKEN_ENV} to the driving service's driver token before sending to the chair"
        )
    return token


def _problem(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    return f"service said {response.status_code}: {error or response.reason_phrase}"


class ChairSender:
    """Posts drive commands to the driving service. Never raises on a failed send."""

    def __init__(
        self,
        service: str,
        token: str,
        hold_ms: int = DEFAULT_HOLD_MS,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token:
            raise ChairOutError("a driver token is needed")
        self.service = service.rstrip("/")
        self.hold_ms = hold_ms
        self._headers = {"authorization": f"Bearer {token}"}
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._clock = clock
        self._last: SendResult | None = None
        self._last_s = float("-inf")

    def send(self, v_mps: float, w_radps: float) -> SendResult:
        """Ask the service to drive at ``v_mps`` forward and ``w_radps`` (positive left).

        Within ``MIN_INTERVAL_S`` of the last send this sends nothing and returns
        that send's result: the service is still holding it.
        """
        now = self._clock()
        if self._last is not None and now - self._last_s < MIN_INTERVAL_S:
            return self._last
        self._last = self._post(max(0.0, float(v_mps)), float(w_radps))
        self._last_s = now
        return self._last

    def _post(self, v: float, w: float) -> SendResult:
        body = {"v_mps": v, "w_radps": w, "hold_ms": self.hold_ms}
        try:
            response = self._client.post(
                f"{self.service}/api/chair/drive", json=body, headers=self._headers
            )
        except httpx.HTTPError as error:
            return SendResult(False, v, w, f"cannot reach the service: {error}")
        if response.status_code != httpx.codes.OK:
            return SendResult(False, v, w, _problem(response))
        data = response.json().get("data") or {}
        held = data.get("test") or {}
        return SendResult(
            True,
            float(held.get("v_mps", v)),
            float(held.get("w_radps", w)),
            None,
            data,
            bool(data.get("limited")),
        )

    def stop(self) -> SendResult:
        """Stop now rather than waiting for the hold to run out. Never throttled."""
        self._last = None
        try:
            response = self._client.delete(f"{self.service}/api/chair/drive", headers=self._headers)
        except httpx.HTTPError as error:
            return SendResult(False, 0.0, 0.0, f"cannot reach the service: {error}")
        if response.status_code != httpx.codes.OK:
            return SendResult(False, 0.0, 0.0, _problem(response))
        return SendResult(True, 0.0, 0.0, None, response.json().get("data"))

    def close(self) -> None:
        self._client.close()
