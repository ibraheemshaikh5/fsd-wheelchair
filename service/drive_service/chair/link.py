"""Chair link state as immutable snapshots, and the one mutable slot that holds them.

Every change makes a new ``ChairLink``. ``ChairLinkStore`` swaps the current one
in place; the socket and the API share a store. All of it runs on the event loop
thread with no awaits inside an update, so no lock is needed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from drive_service.chair.commander import BenchCommand, bench_active
from drive_service.chair.protocol import ChairHello, ChairStatus, DriveCommand

MS_PER_S = 1000.0


@dataclass(frozen=True, slots=True)
class ReceivedStatus:
    status: ChairStatus
    received_mono: float


def _takes_over(status: ChairStatus) -> bool:
    """The rider or the e-stop has the chair: any bench test must not resume afterwards."""
    return status.estop or status.joystick_active or status.mode == "manual"


@dataclass(frozen=True, slots=True)
class ChairLink:
    lease: int = 0
    connected: bool = False
    hello: ChairHello | None = None
    status: ReceivedStatus | None = None
    # Kept across connections so seq only ever counts up while the service runs.
    last_command: DriveCommand | None = None
    bench: BenchCommand | None = None
    rejected: int = 0

    def connected_as(self, lease: int) -> ChairLink:
        return replace(
            self, lease=lease, connected=True, hello=None, status=None, bench=None, rejected=0
        )

    def disconnected(self, lease: int) -> ChairLink:
        if lease != self.lease:
            return self
        return replace(self, connected=False, hello=None, status=None, bench=None)

    def with_hello(self, hello: ChairHello) -> ChairLink:
        return replace(self, hello=hello)

    def with_status(self, status: ChairStatus, now_mono: float) -> ChairLink:
        bench = None if _takes_over(status) else self.bench
        return replace(self, status=ReceivedStatus(status, now_mono), bench=bench)

    def with_command(self, command: DriveCommand) -> ChairLink:
        return replace(self, last_command=command)

    def with_bench(self, bench: BenchCommand) -> ChairLink:
        return replace(self, bench=bench)

    def without_bench(self) -> ChairLink:
        return replace(self, bench=None)

    def with_rejected(self) -> ChairLink:
        return replace(self, rejected=self.rejected + 1)


class ChairLinkStore:
    """Holds the current snapshot. The only mutable object in the chair package."""

    def __init__(self) -> None:
        self._state = ChairLink()

    @property
    def state(self) -> ChairLink:
        return self._state

    def update(self, change: Callable[[ChairLink], ChairLink]) -> ChairLink:
        self._state = change(self._state)
        return self._state

    def claim(self) -> int:
        """Give a new connection the link; any older one loses its lease."""
        lease = self._state.lease + 1
        self.update(lambda link: link.connected_as(lease))
        return lease

    def holds(self, lease: int) -> bool:
        return self._state.connected and self._state.lease == lease


def status_age_ms(link: ChairLink, now_mono: float) -> int | None:
    """How old the last chair status is, in ms, or None before the first one."""
    if link.status is None:
        return None
    return round((now_mono - link.status.received_mono) * MS_PER_S)


def status_fresh(link: ChairLink, now_mono: float, stale_ms: float) -> bool:
    """True when the chair has reported within ``stale_ms``: its e-stop and joystick are known."""
    age_ms = status_age_ms(link, now_mono)
    return age_ms is not None and age_ms <= stale_ms


def _bench_view(bench: BenchCommand | None, now_mono: float) -> dict[str, Any]:
    if bench is None or not bench_active(bench, now_mono):
        return {
            "active": False, "source": None, "v_mps": 0.0, "w_radps": 0.0, "remaining_ms": 0,
        }  # fmt: skip
    return {
        "active": True,
        "source": bench.source,
        "v_mps": bench.v_mps,
        "w_radps": bench.w_radps,
        "remaining_ms": round((bench.expires_mono - now_mono) * MS_PER_S),
    }


def chair_view(link: ChairLink, now_mono: float, stale_ms: float) -> dict[str, Any]:
    """What GET /api/chair shows: JSON-ready, with the status aged as of ``now_mono``."""
    return {
        "connected": link.connected,
        "hello": link.hello.model_dump() if link.hello else None,
        "status": link.status.status.model_dump() if link.status else None,
        "status_age_ms": status_age_ms(link, now_mono),
        "status_fresh": status_fresh(link, now_mono, stale_ms),
        "last_command": link.last_command.model_dump() if link.last_command else None,
        "test": _bench_view(link.bench, now_mono),
        "rejected": link.rejected,
    }
