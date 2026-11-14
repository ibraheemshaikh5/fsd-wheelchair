"""WebSocket endpoints: /ws/phone (sensor ingest) and /ws/viewer (live debug feed).

Only one phone feeds the service. The newest /ws/phone connection always wins:
the older one is told it was superseded and closes itself with 4410. Every send
and close on a socket happens in that socket's own task, never from another
connection's task, so frames on the wire can never interleave.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from drive_service.config import Settings
from drive_service.destination import DestinationStore
from drive_service.hub import Hub
from drive_service.origin import origin_allowed
from drive_service.protocol import ErrorReply, Frame, Pong, ProtocolError, parse_frame, parse_text
from drive_service.recorder import Recorder
from drive_service.state import Stamped
from drive_service.status import live_status
from drive_service.timeutil import TimeSource

log = logging.getLogger(__name__)

CLOSE_POLICY_VIOLATION = 1008
CLOSE_PHONE_IDLE = 4408
CLOSE_PHONE_REPLACED = 4410
CLOSE_TOO_MANY_VIEWERS = 4429

DISCONNECT = "websocket.disconnect"

# Builds the status to send a viewer, as of the moment it is called.
type StatusNow = Callable[[], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _Close:
    code: int
    reason: str


REPLACED = _Close(CLOSE_PHONE_REPLACED, "replaced by a newer phone connection")
TOO_MANY_ERRORS = _Close(CLOSE_POLICY_VIOLATION, "too many invalid messages")


def _idle(timeout_s: float) -> _Close:
    return _Close(CLOSE_PHONE_IDLE, f"no data for {timeout_s:g} s")


async def _close_quietly(ws: WebSocket, close: _Close) -> None:
    """Close from this socket's own task; the peer may already be gone."""
    try:
        await ws.close(code=close.code, reason=close.reason)
    except (WebSocketDisconnect, RuntimeError) as error:
        log.debug("socket was already closed when sending %d: %r", close.code, error)


def _discard(task: asyncio.Future[Any]) -> None:
    """Cancel a task we no longer need, retrieving any error it already raised."""
    if not task.done():
        task.cancel()
    elif not task.cancelled() and task.exception() is not None:
        log.debug("ignored error from an abandoned socket wait: %r", task.exception())


async def _next_phone_event(
    ws: WebSocket, superseded: asyncio.Event, timeout_s: float
) -> dict[str, Any] | _Close:
    """The next raw message from the phone, or why this connection must close."""
    receive = asyncio.ensure_future(ws.receive())
    stop = asyncio.ensure_future(superseded.wait())
    try:
        done, _ = await asyncio.wait(
            {receive, stop}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
    except BaseException:
        # Cancelled (shutdown): leave no wait behind on the socket.
        _discard(receive)
        raise
    finally:
        _discard(stop)
    if stop in done:
        _discard(receive)
        return REPLACED
    if receive in done:
        return receive.result()
    _discard(receive)
    return _idle(timeout_s)


def _dispatch(hub: Hub, settings: Settings, message: dict[str, Any]) -> Pong | None:
    """Validate one raw WebSocket message and hand it to the hub."""
    data = message.get("bytes")
    if data is not None:
        frame = parse_frame(data, settings.max_frame_bytes, settings.max_frame_header_bytes)
        hub.handle_frame(frame)
        return None
    text = message.get("text")
    if text is None:
        raise ProtocolError("empty message")
    return hub.handle_message(parse_text(text, settings.max_text_bytes))


def _try_dispatch(
    hub: Hub, settings: Settings, message: dict[str, Any]
) -> Pong | ProtocolError | None:
    try:
        return _dispatch(hub, settings, message)
    except ProtocolError as error:
        hub.note_rejected()
        log.warning("rejected phone message: %s", error)
        return error


async def _reply(
    ws: WebSocket, outcome: Pong | ProtocolError | None, errors: int, settings: Settings
) -> int | None:
    """Answer the phone. Returns the updated error streak, or None to stop serving."""
    if isinstance(outcome, ProtocolError):
        await ws.send_text(ErrorReply(message=str(outcome)).to_wire())
        if errors + 1 >= settings.max_consecutive_errors:
            log.error("closing phone connection after %d bad messages", errors + 1)
            await _close_quietly(ws, TOO_MANY_ERRORS)
            return None
        return errors + 1
    if outcome is not None:
        await ws.send_text(outcome.to_wire())
    return 0


async def _serve_phone(
    ws: WebSocket, hub: Hub, settings: Settings, lease: int, superseded: asyncio.Event
) -> None:
    errors: int | None = 0
    while errors is not None:
        event = await _next_phone_event(ws, superseded, settings.phone_idle_timeout_s)
        # Re-checked with no await before the dispatch below, so a message already
        # queued on a replaced socket can never be applied after the takeover.
        if not isinstance(event, _Close) and not hub.holds_phone(lease):
            event = REPLACED
        if isinstance(event, _Close):
            log.info("closing phone connection: %s", event.reason)
            await _close_quietly(ws, event)
            return
        if event["type"] == DISCONNECT:
            return
        outcome = _try_dispatch(hub, settings, event)
        errors = await _reply(ws, outcome, errors, settings)


async def _send_viewer_update(
    ws: WebSocket, hub: Hub, status_now: StatusNow, last_sent: Stamped[Frame] | None
) -> Stamped[Frame] | None:
    """Send the status, then the latest frame if it is new. Returns what was last sent."""
    # No await between these two lines, so the frame matches the status sent.
    status = status_now()
    snapshot = hub.snapshot
    await ws.send_text(json.dumps(status))
    if snapshot.frame is not None and snapshot.frame is not last_sent:
        await ws.send_bytes(snapshot.frame.value.jpeg)
        return snapshot.frame
    return last_sent


async def _idle_until(ws: WebSocket, loop: asyncio.AbstractEventLoop, deadline: float) -> bool:
    """Wait for ``deadline``, ignoring anything the viewer sends. False once it leaves."""
    while (remaining := deadline - loop.time()) > 0:
        try:
            message = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except TimeoutError:
            return True
        if message["type"] == DISCONNECT:
            return False
    return True


async def _serve_viewer(ws: WebSocket, hub: Hub, status_now: StatusNow, settings: Settings) -> None:
    # Paced by the event loop's own clock (time_source may be a fake in tests), so
    # messages from the viewer can never make the service send faster.
    loop = asyncio.get_running_loop()
    interval_s = 1.0 / settings.viewer_fps
    next_tick = loop.time()
    last_sent: Stamped[Frame] | None = None
    while True:
        last_sent = await _send_viewer_update(ws, hub, status_now, last_sent)
        next_tick = max(next_tick + interval_s, loop.time())
        if not await _idle_until(ws, loop, next_tick):
            return


def build_router(
    hub: Hub,
    recorder: Recorder,
    destinations: DestinationStore,
    settings: Settings,
    time_source: TimeSource,
) -> APIRouter:
    router = APIRouter()

    def status_now() -> dict[str, Any]:
        return live_status(hub, recorder, destinations, time_source.mono(), settings)

    viewers = {"count": 0}
    # Set to tell the current phone connection that a newer one took over.
    current_phone: dict[str, asyncio.Event | None] = {"superseded": None}

    async def refuse_foreign_origin(ws: WebSocket) -> bool:
        if origin_allowed(ws.headers, settings.allowed_origins):
            return False
        log.warning(
            "refused %s from origin %r (host %r); add it to DRIVE_ALLOWED_ORIGINS if it is yours",
            ws.url.path,
            ws.headers.get("origin"),
            ws.headers.get("host"),
        )
        # Closing before accept() refuses the handshake: uvicorn answers HTTP 403
        # and the browser sees close code 1006. The socket is never opened.
        await ws.close(code=CLOSE_POLICY_VIOLATION)
        return True

    def take_phone_slot() -> tuple[int, asyncio.Event]:
        """Supersede the current phone connection and claim the slot (no awaits)."""
        previous = current_phone["superseded"]
        if previous is not None:
            previous.set()
        superseded = asyncio.Event()
        current_phone["superseded"] = superseded
        return hub.attach_phone(), superseded

    @router.websocket("/ws/phone")
    async def phone_socket(ws: WebSocket) -> None:
        if await refuse_foreign_origin(ws):
            return
        await ws.accept()
        lease, superseded = take_phone_slot()
        log.info("phone connected (lease %d)", lease)
        try:
            await _serve_phone(ws, hub, settings, lease, superseded)
        except WebSocketDisconnect:
            pass
        finally:
            hub.detach_phone(lease)
            log.info("phone disconnected (lease %d)", lease)

    @router.websocket("/ws/viewer")
    async def viewer_socket(ws: WebSocket) -> None:
        if await refuse_foreign_origin(ws):
            return
        await ws.accept()
        if viewers["count"] >= settings.max_viewers:
            await ws.close(code=CLOSE_TOO_MANY_VIEWERS, reason="too many viewers")
            return
        viewers["count"] += 1
        try:
            await _serve_viewer(ws, hub, status_now, settings)
        except WebSocketDisconnect:
            pass
        finally:
            viewers["count"] -= 1

    return router
