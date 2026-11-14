"""WebSocket client: driving service <-> microcontroller.

One session = one connection to /ws/chair. Each session starts by telling the
microcontroller to stop (which disarms it until the service sends a zero
command), says hello, then forwards drive commands and reports chair status.
Only the newest command is forwarded: commands that queued up behind a slow
Bridge call or a network stall are dropped, never replayed. Any disconnect or
error ends the session and stops the chair immediately; the client then
reconnects with exponential backoff and jitter.
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .backoff import Backoff
from .config import LinkConfig
from .latest import LatestCommand
from .mcu import McuError, McuPort
from .protocol import (
    DriveCommand,
    ProtocolError,
    build_chair_message,
    build_hello,
    parse_server_message,
)

log = logging.getLogger(__name__)

OPEN_TIMEOUT_S = 5.0
PING_INTERVAL_S = 2.0
PING_TIMEOUT_S = 2.0
CLOSE_TIMEOUT_S = 1.0
MAX_MESSAGE_BYTES = 64 * 1024
STATUS_LOG_EVERY = 50  # Log every Nth consecutive status failure.
CONNECTION_ERRORS = (OSError, TimeoutError, WebSocketException)


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class ChairLink:
    def __init__(
        self,
        config: LinkConfig,
        mcu: McuPort,
        *,
        backoff: Backoff | None = None,
        rand: Callable[[], float] = random.random,
        clock_ms: Callable[[], int] = wall_clock_ms,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._config = config
        self._mcu = mcu
        self._backoff = backoff or Backoff()
        self._rand = rand
        self._clock_ms = clock_ms
        self._sleep = sleep
        self._mcu_lock = asyncio.Lock()

    async def run_forever(self) -> None:
        """Keeps a session up until cancelled. The chair is stopped between sessions."""
        attempt = 0
        while True:
            worked = False
            try:
                worked = await self._session()
                log.info("service closed the connection")
            except InvalidStatus as exc:
                log.error(
                    "service refused the chair (HTTP %s): check the token", exc.response.status_code
                )
            except CONNECTION_ERRORS as exc:
                log.warning("connection to service lost: %r", exc)
            except McuError as exc:
                log.error("microcontroller error, ending session: %s", exc)
            except Exception:
                log.exception("unexpected error in chair link session")
            finally:
                self._stop_now()
            attempt = 0 if worked else attempt + 1
            delay = self._backoff.delay(max(attempt - 1, 0), self._rand)
            log.info("reconnecting in %.2f s", delay)
            await self._sleep(delay)

    def _stop_now(self) -> None:
        """Stops the chair without waiting for the event loop or the lock."""
        try:
            self._mcu.stop()
        except McuError as exc:
            log.error("could not tell the microcontroller to stop: %s", exc)

    async def _mcu_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        async with self._mcu_lock:
            return await asyncio.to_thread(fn, *args)

    async def _session(self) -> bool:
        """Runs one connection. Returns True if at least one command was forwarded."""
        headers = {"Authorization": f"Bearer {self._config.token}"}
        async with connect(
            self._config.service_url,
            additional_headers=headers,
            open_timeout=OPEN_TIMEOUT_S,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            close_timeout=CLOSE_TIMEOUT_S,
            max_size=MAX_MESSAGE_BYTES,
        ) as ws:
            log.info("connected to %s", self._config.service_url)
            self._stop_now()
            limits = await self._mcu_call(self._mcu.limits)
            await ws.send(json.dumps(build_hello(self._config.firmware, limits)))
            slot = LatestCommand()
            forwarder = _Forwarder(self, slot)
            await _first_to_finish(
                _receive(ws, slot, self._stop_now), forwarder.run(), self._report_status(ws)
            )
            return forwarder.forwarded > 0

    async def _forward(self, cmd: DriveCommand) -> bool:
        """Sends one command to the MCU. Returns True if it was delivered."""
        try:
            accepted = await self._mcu_call(self._mcu.drive, cmd)
        except McuError as exc:
            log.error("drive seq %d not delivered, stopping: %s", cmd.seq, exc)
            self._stop_now()
            return False
        if not accepted:
            log.debug("microcontroller ignored drive seq %d", cmd.seq)
        return True

    async def _report_status(self, ws: ClientConnection) -> None:
        period_s = 1.0 / self._config.status_hz
        failures = 0
        while True:
            try:
                status = await self._mcu_call(self._mcu.status)
            except McuError as exc:
                if failures % STATUS_LOG_EVERY == 0:
                    log.warning("status read failed (%d in a row): %s", failures + 1, exc)
                failures += 1
            else:
                if failures:
                    log.info("status read recovered after %d failures", failures)
                failures = 0
                await ws.send(json.dumps(build_chair_message(status, self._clock_ms())))
            await asyncio.sleep(period_s)


async def _receive(ws: ClientConnection, slot: LatestCommand, stop: Callable[[], None]) -> None:
    """Reads service messages as fast as they come; only the newest command is kept."""
    async for raw in ws:
        try:
            cmd = parse_server_message(raw)
        except ProtocolError as exc:
            log.warning("bad message from service, stopping: %s", exc)
            slot.clear()
            stop()
            continue
        if cmd is not None:
            slot.put(cmd)


class _Forwarder:
    """Sends the newest command to the MCU, never one older than the last sent."""

    def __init__(self, link: ChairLink, slot: LatestCommand):
        self._link = link
        self._slot = slot
        self._last_seq: int | None = None
        self.forwarded = 0

    async def run(self) -> None:
        while True:
            cmd = await self._slot.take()
            if self._last_seq is not None and cmd.seq <= self._last_seq:
                log.debug("dropping drive seq %d (last sent %d)", cmd.seq, self._last_seq)
                continue
            self._last_seq = cmd.seq
            if await self._link._forward(cmd):
                self.forwarded += 1


async def _first_to_finish(*coros: Awaitable[None]) -> None:
    """Runs coroutines until one ends; cancels the rest and re-raises its error."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        task.result()
