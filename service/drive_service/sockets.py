"""WebSocket endpoints: /ws/phone (sensor ingest) and /ws/viewer (live debug feed).

Every binary frame the phone sends is answered with an ack carrying its seq,
whether it was stored, only rate-limited out of the recording, or refused with a
readable header, so the phone can cap the frames it has in flight. A frame whose
header cannot be read gets only the error reply.

Only one phone feeds the service. The newest /ws/phone connection wins: the
older one is told it was superseded and closes itself with 4410. The exception is
a phone signed in to Tailscale as one of ``priority_users``: while it holds the
slot, any other phone is turned away with 4409 instead. Every send
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
from drive_service.protocol import (
    Ack,
    ErrorReply,
    Frame,
    FrameError,
    Pong,
    ProtocolError,
    parse_frame,
    parse_text,
)
from drive_service.recorder import Recorder
from drive_service.state import Stamped
from drive_service.status import live_status
from drive_service.timeutil import TimeSource

log = logging.getLogger(__name__)

CLOSE_POLICY_VIOLATION = 1008
CLOSE_PHONE_IDLE = 4408
CLOSE_PHONE_BUSY = 4409
CLOSE_PHONE_REPLACED = 4410
CLOSE_TOO_MANY_VIEWERS = 4429

DISCONNECT = "websocket.disconnect"
# Set by `tailscale serve` to the login of the tailnet user making the request.
TAILSCALE_LOGIN_HEADER = "tailscale-user-login"

# Builds the status to send a viewer, as of the moment it is called.
type StatusNow = Callable[[], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _Close:
    code: int
    reason: str


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What to send the phone after one message, in this order: error, pong, ack."""

    error: ProtocolError | None = None
    reply: Pong | None = None
    ack: Ack | None = None


@dataclass(frozen=True, slots=True)
class _Holder:
    """The connection that holds the phone slot."""

    lease: int
    superseded: asyncio.Event
    priority: bool


REPLACED = _Close(CLOSE_PHONE_REPLACED, "replaced by a newer phone connection")
BUSY = _Close(CLOSE_PHONE_BUSY, "another phone has priority")
TOO_MANY_ERRORS = _Close(CLOSE_POLICY_VIOLATION, "too many invalid messages")


def _idle(timeout_s: float) -> _Close:
    return _Close(CLOSE_PHONE_IDLE, f"no data for {timeout_s:g} s")


async def _close_quietly(ws: WebSocket, close: _Close) -> None:
    """Close from this socket's own task; the peer may already be gone."""
    try:
        await ws.close(code=close.code, reason=close.reason)
    except (WebSocketDisconnect, RuntimeError) as error:
        log.debug("socket was already closed when sending %d: %r", close.code, error)


def _is_priority(ws: WebSocket, settings: Settings) -> bool:
    login = ws.headers.get(TAILSCALE_LOGIN_HEADER, "").strip().lower()
    return bool(login) and login in settings.priority_users


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


def _dispatch(hub: Hub, settings: Settings, message: dict[str, Any]) -> _Outcome:
    """Validate one raw WebSocket message and hand it to the hub."""
    data = message.get("bytes")
    if data is not None:
        frame = parse_frame(data, settings.max_frame_bytes, settings.max_frame_header_bytes)
        return _Outcome(ack=hub.handle_frame(frame))
    text = message.get("text")
    if text is None:
        raise ProtocolError("empty message")
    return _Outcome(reply=hub.handle_message(parse_text(text, settings.max_text_bytes)))


def _try_dispatch(hub: Hub, settings: Settings, message: dict[str, Any]) -> _Outcome:
    try:
        return _dispatch(hub, settings, message)
    except ProtocolError as error:
        hub.note_rejected()
        log.warning("rejected phone message: %s", error)
        seq = error.seq if isinstance(error, FrameError) else None
        return _Outcome(error=error, ack=None if seq is None else hub.ack_frame(seq))


async def _send(ws: WebSocket, outcome: _Outcome) -> None:
    if outcome.error is not None:
        await ws.send_text(ErrorReply(message=str(outcome.error)).to_wire())
    if outcome.reply is not None:
        await ws.send_text(outcome.reply.to_wire())
    if outcome.ack is not None:
        await ws.send_text(outcome.ack.to_wire())


async def _reply(ws: WebSocket, outcome: _Outcome, errors: int, settings: Settings) -> int | None:
    """Answer the phone. Returns the updated error streak, or None to stop serving."""
    await _send(ws, outcome)
    if outcome.error is None:
        return 0
    if errors + 1 >= settings.max_consecutive_errors:
        log.error("closing phone connection after %d bad messages", errors + 1)
        await _close_quietly(ws, TOO_MANY_ERRORS)
        return None
    return errors + 1


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
    # Who holds the phone slot; its event is set to tell it a newer one took over.
    current_phone: dict[str, _Holder | None] = {"holder": None}

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

    def slot_reserved() -> bool:
        """Whether a priority phone holds the slot, so others must not take it."""
        holder = current_phone["holder"]
        return holder is not None and holder.priority and hub.holds_phone(holder.lease)

    def take_phone_slot(priority: bool) -> _Holder:
        """Supersede the current phone connection and claim the slot (no awaits)."""
        previous = current_phone["holder"]
        if previous is not None:
            previous.superseded.set()
        holder = _Holder(lease=hub.attach_phone(), superseded=asyncio.Event(), priority=priority)
        current_phone["holder"] = holder
        return holder

    @router.websocket("/ws/phone")
    async def phone_socket(ws: WebSocket) -> None:
        if await refuse_foreign_origin(ws):
            return
        await ws.accept()
        priority = _is_priority(ws, settings)
        # No await between this check and taking the slot, so two phones cannot race past it.
        if not priority and slot_reserved():
            log.info("turned away a phone: a priority phone holds the slot")
            await _close_quietly(ws, BUSY)
            return
        holder = take_phone_slot(priority)
        lease = holder.lease
        log.info("phone connected (lease %d%s)", lease, ", priority" if priority else "")
        try:
            await _serve_phone(ws, hub, settings, lease, holder.superseded)
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
