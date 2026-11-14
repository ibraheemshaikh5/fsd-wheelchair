"""The /ws/chair endpoint: the board connects here and is sent drive commands at rate_hz.

Refused before accept (HTTP 403, close 1008) unless the board presents the
configured token as ``Authorization: Bearer <token>``. With no token configured
every chair is refused. Any Origin header is refused too: the board sends none,
and a web page must never be able to drive the motors.

One chair at a time: the newest authenticated connection wins and the older
socket closes itself with 4410. Every send and close happens in the socket's own
task, and the lease is checked right before each send, so a replaced socket never
sends another command.

Late commands end the link rather than pile up: a send blocked for over ttl_ms
closes with 4408, and a status whose applied_seq trails the newest command by
more than max_command_lag_ms closes with 4409. Either way the bench test is
cleared. Bytes already queued still reach the board, so the board must also drop
a drive whose ``t`` is too old (docs/chair-link.md, "Late commands").
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, WebSocket
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from drive_service.chair.commander import (
    bench_active,
    command_lag_ms,
    effective_caps,
    next_command,
)
from drive_service.chair.config import MS_PER_S, ChairSettings
from drive_service.chair.link import ChairLink, ChairLinkStore, status_fresh
from drive_service.chair.protocol import (
    ChairHello,
    ChairProtocolError,
    ChairStatus,
    parse_chair_message,
)
from drive_service.timeutil import TimeSource

log = logging.getLogger(__name__)

CLOSE_POLICY_VIOLATION = 1008
# Silent board, or one that stopped reading so a send blocked for over ttl_ms.
CLOSE_CHAIR_IDLE = 4408
# The board's applied_seq trails the newest command by more than max_command_lag_ms.
CLOSE_CHAIR_LAGGING = 4409
CLOSE_CHAIR_REPLACED = 4410
DISCONNECT = "websocket.disconnect"
BEARER_PREFIX = "Bearer "


class _Closing(Exception):
    """This connection must close with ``code``."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def bearer_token_ok(headers: Mapping[str, str], token: SecretStr | None) -> bool:
    """True only when a token is configured and the request presents exactly it."""
    if token is None:
        return False
    value = headers.get("authorization", "")
    if not value.startswith(BEARER_PREFIX):
        return False
    presented = value[len(BEARER_PREFIX) :].strip().encode("utf-8")
    return hmac.compare_digest(presented, token.get_secret_value().encode("utf-8"))


def _refusal(headers: Mapping[str, str], settings: ChairSettings) -> str | None:
    """Why a chair handshake must be refused, or None to accept it. Never logs the token."""
    if settings.token is None:
        return "no chair token configured (set DRIVE_CHAIR_TOKEN)"
    if "origin" in headers:
        return f"browser origin {headers.get('origin')!r} may not drive the chair"
    if not bearer_token_ok(headers, settings.token):
        return "missing or wrong bearer token"
    return None


async def _close_quietly(ws: WebSocket, code: int, reason: str, timeout_s: float) -> None:
    """Send the close frame, giving up after ``timeout_s`` if the board is not reading."""
    try:
        await asyncio.wait_for(ws.close(code=code, reason=reason), timeout=timeout_s)
    except TimeoutError:
        log.warning("chair did not take the close frame (%d) within %g s", code, timeout_s)
    except (WebSocketDisconnect, RuntimeError) as error:
        log.debug("chair socket already closed when sending %d: %r", code, error)


class _ChairSession:
    """One accepted chair connection, served from its own task."""

    def __init__(
        self,
        ws: WebSocket,
        store: ChairLinkStore,
        settings: ChairSettings,
        clock: TimeSource,
        lease: int,
    ) -> None:
        self.ws = ws
        self.store = store
        self.settings = settings
        self.clock = clock
        self.lease = lease
        self.errors = 0
        self.send_timeout_s = settings.ttl_ms / MS_PER_S

    def _check_lease(self) -> None:
        if not self.store.holds(self.lease):
            raise _Closing(CLOSE_CHAIR_REPLACED, "replaced by a newer chair connection")

    def _settle_bench(self, now: float) -> ChairLink:
        """Drop a bench test that has run out, or whose chair has stopped reporting."""
        link = self.store.state
        if link.bench is None:
            return link
        if not bench_active(link.bench, now):
            log.info("bench test finished; sending stop")
            return self.store.update(lambda current: current.without_bench())
        if not status_fresh(link, now, self.settings.status_stale_ms):
            log.warning("chair status went stale during a bench test; sending stop")
            return self.store.update(lambda current: current.without_bench())
        return link

    async def send_command(self) -> None:
        now = self.clock.mono()
        link = self._settle_bench(now)
        command = next_command(
            now_mono=now,
            wall_ms=self.clock.wall_ms(),
            bench=link.bench,
            last_seq=link.last_command.seq if link.last_command else None,
            caps=effective_caps(self.settings, link.hello),
            ttl_ms=self.settings.ttl_ms,
        )
        # No await between the lease check, the record and the send.
        self._check_lease()
        self.store.update(lambda current: current.with_command(command))
        # A board that stops reading must not let commands pile up to be driven late.
        try:
            await asyncio.wait_for(
                self.ws.send_text(command.to_wire()), timeout=self.send_timeout_s
            )
        except TimeoutError as error:
            raise _Closing(
                CLOSE_CHAIR_IDLE, f"send blocked for over {self.settings.ttl_ms} ms"
            ) from error

    def _check_lag(self, status: ChairStatus) -> None:
        last = self.store.state.last_command
        lag_ms = command_lag_ms(
            last.seq if last else None, status.applied_seq, self.settings.rate_hz
        )
        if lag_ms > self.settings.max_command_lag_ms:
            raise _Closing(
                CLOSE_CHAIR_LAGGING,
                f"chair applies commands {lag_ms:.0f} ms late "
                f"(limit {self.settings.max_command_lag_ms} ms)",
            )

    def _apply(self, message: dict[str, Any]) -> None:
        text = message.get("text")
        if text is None:
            raise ChairProtocolError("chair messages must be text")
        parsed = parse_chair_message(text, self.settings.max_text_bytes)
        link = self.store.state
        if isinstance(parsed, ChairHello):
            if link.hello is not None:
                raise ChairProtocolError("hello sent twice on one connection")
            log.info(
                "chair hello: firmware %s, caps %.2f m/s, %.2f rad/s",
                parsed.firmware,
                parsed.max_speed_mps,
                parsed.max_turn_radps,
            )
            self.store.update(lambda current: current.with_hello(parsed))
            return
        if link.hello is None:
            raise ChairProtocolError("chair status before hello")
        now = self.clock.mono()
        self.store.update(lambda current: current.with_status(parsed, now))
        self._check_lag(parsed)

    def handle(self, message: dict[str, Any]) -> None:
        """Apply one message from the board; close after too many bad ones in a row."""
        self._check_lease()
        try:
            self._apply(message)
        except ChairProtocolError as error:
            self.errors += 1
            self.store.update(lambda current: current.with_rejected())
            log.warning("rejected chair message: %s", error)
            if self.errors >= self.settings.max_consecutive_errors:
                raise _Closing(CLOSE_POLICY_VIOLATION, "too many invalid messages") from error
            return
        self.errors = 0


async def _receive_until(
    session: _ChairSession, loop: asyncio.AbstractEventLoop, deadline: float, idle_deadline: float
) -> float | None:
    """Handle board messages until ``deadline``. Returns the new idle deadline, None on leave."""
    idle_s = session.settings.idle_timeout_ms / MS_PER_S
    while (remaining := deadline - loop.time()) > 0:
        if loop.time() >= idle_deadline:
            raise _Closing(CLOSE_CHAIR_IDLE, f"no message for {idle_s:g} s")
        try:
            message = await asyncio.wait_for(session.ws.receive(), timeout=remaining)
        except TimeoutError:
            break
        if message["type"] == DISCONNECT:
            return None
        idle_deadline = loop.time() + idle_s
        session.handle(message)
    return idle_deadline


async def _serve_chair(session: _ChairSession) -> None:
    # Paced by the event loop's clock (the time source may be a fake in tests), so
    # nothing the board sends can make the service send faster or slower.
    loop = asyncio.get_running_loop()
    interval_s = 1.0 / session.settings.rate_hz
    next_tick = loop.time()
    idle_deadline: float | None = next_tick + session.settings.idle_timeout_ms / MS_PER_S
    try:
        while idle_deadline is not None:
            await session.send_command()
            next_tick = max(next_tick + interval_s, loop.time())
            idle_deadline = await _receive_until(session, loop, next_tick, idle_deadline)
    except _Closing as closing:
        log.info("closing chair connection: %s", closing.reason)
        await _close_quietly(session.ws, closing.code, closing.reason, session.send_timeout_s)


def build_ws_router(store: ChairLinkStore, settings: ChairSettings, clock: TimeSource) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/chair")
    async def chair_socket(ws: WebSocket) -> None:
        refusal = _refusal(ws.headers, settings)
        if refusal is not None:
            log.warning("refused chair connection from %s: %s", ws.client, refusal)
            # Closing before accept() refuses the handshake with HTTP 403.
            await ws.close(code=CLOSE_POLICY_VIOLATION)
            return
        await ws.accept()
        lease = store.claim()
        log.info("chair connected (lease %d)", lease)
        try:
            await _serve_chair(_ChairSession(ws, store, settings, clock, lease))
        except WebSocketDisconnect:
            log.debug("chair socket dropped (lease %d)", lease)
        finally:
            store.update(lambda link: link.disconnected(lease))
            log.info("chair disconnected (lease %d)", lease)

    return router
