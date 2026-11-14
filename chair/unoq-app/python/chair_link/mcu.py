"""The microcontroller, reached through the Arduino Router Bridge.

The client only sees the McuPort protocol, so tests use a fake and main.py
wires in arduino.app_utils.Bridge.call.
"""

from collections.abc import Callable
from typing import Any, Protocol

from .protocol import DriveCommand, McuLimits, McuStatus, ProtocolError

# Bridge.call blocks the calling thread until the MCU answers. Keep it short:
# the sketch's own watchdog stops the chair long before this matters.
BRIDGE_TIMEOUT_S = 1


class McuError(RuntimeError):
    """A Bridge call to the microcontroller failed or returned garbage."""


class McuPort(Protocol):
    def drive(self, cmd: DriveCommand) -> bool: ...
    def stop(self) -> None: ...
    def status(self) -> McuStatus: ...
    def limits(self) -> McuLimits: ...


BridgeCall = Callable[..., Any]


class BridgeMcu:
    """McuPort over Bridge.call(method, *params, timeout=...)."""

    def __init__(self, call: BridgeCall, timeout_s: float = BRIDGE_TIMEOUT_S):
        self._call = call
        self._timeout_s = timeout_s

    def _invoke(self, method: str, *params: Any) -> Any:
        try:
            return self._call(method, *params, timeout=self._timeout_s)
        except Exception as exc:  # Bridge raises ValueError/TimeoutError/RuntimeError.
            raise McuError(f"Bridge call '{method}' failed: {exc}") from exc

    def drive(self, cmd: DriveCommand) -> bool:
        reply = self._invoke(
            "drive", int(cmd.seq), float(cmd.v_mps), float(cmd.w_radps), int(cmd.ttl_ms)
        )
        return bool(reply)

    def stop(self) -> None:
        self._invoke("stop")

    def status(self) -> McuStatus:
        try:
            return McuStatus.from_array(self._invoke("status"))
        except ProtocolError as exc:
            raise McuError(f"bad status reply: {exc}") from exc

    def limits(self) -> McuLimits:
        try:
            return McuLimits.from_array(self._invoke("limits"))
        except ProtocolError as exc:
            raise McuError(f"bad limits reply: {exc}") from exc
