"""/ws/camera: camera frames from a sender that is not the phone.

Today the sender is ``scripts/camera_sender.py`` reading a USB camera on the Mac;
later the same sender runs on the chair's board with the camera plugged in there.
It speaks the phone protocol's subset that a camera needs: ``hello`` (camera
info), ``ping`` (clock sync, kept apart from the phone's) and binary frames,
each answered with an ``ack``. Anything else is refused with an ``error``.

Not a browser endpoint: the sender must present ``Authorization: Bearer <token>``
and no ``Origin``. The handshake is refused (1008 before
accept, HTTP 403) when frames come from the phone, with no token configured,
with a wrong token, or from any web page. One camera at a time: the newest
connection takes over and the older one is closed with 4410.
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

from drive_service.camera.config import CameraSettings
from drive_service.config import Settings
from drive_service.hub import Hub
from drive_service.protocol import (
    FrameError,
    Hello,
    Ping,
    ProtocolError,
    parse_frame,
    parse_text,
)
from drive_service.sockets import (
    _Close,
    _close_quietly,
    _next_phone_event,
    _Outcome,
    _reply,
)

log = logging.getLogger(__name__)

CLOSE_POLICY_VIOLATION = 1008
CLOSE_CAMERA_REPLACED = 4410
DISCONNECT = "websocket.disconnect"

REPLACED = _Close(CLOSE_CAMERA_REPLACED, "replaced by a newer camera connection")
BEARER_PREFIX = "Bearer "


def bearer_token_ok(headers: Mapping[str, str], token: SecretStr | None) -> bool:
    """True only when a token is configured and the request presents exactly it."""
    if token is None:
        return False
    value = headers.get("authorization", "")
    if not value.startswith(BEARER_PREFIX):
        return False
    presented = value[len(BEARER_PREFIX) :].strip().encode("utf-8")
    return hmac.compare_digest(presented, token.get_secret_value().encode("utf-8"))


def _refusal(headers: Mapping[str, str], settings: CameraSettings) -> str | None:
    """Why a camera handshake must be refused, or None to accept it. Never logs the token."""
    if settings.source != "external":
        return "frames come from the phone (set DRIVE_CAMERA_SOURCE=external)"
    if settings.token is None:
        return "no camera token configured (set DRIVE_CAMERA_TOKEN)"
    if "origin" in headers:
        return f"browser origin {headers.get('origin')!r} may not send camera frames"
    if not bearer_token_ok(headers, settings.token):
        return "missing or wrong bearer token"
    return None


def _dispatch(hub: Hub, settings: Settings, message: dict[str, Any]) -> _Outcome:
    """Validate one raw message from the camera and hand it to the hub."""
    data = message.get("bytes")
    if data is not None:
        frame = parse_frame(data, settings.max_frame_bytes, settings.max_frame_header_bytes)
        return _Outcome(ack=hub.handle_camera_frame(frame))
    text = message.get("text")
    if text is None:
        raise ProtocolError("empty message")
    parsed = parse_text(text, settings.max_text_bytes)
    if isinstance(parsed, Ping):
        return _Outcome(reply=hub.handle_camera_ping(parsed))
    if isinstance(parsed, Hello):
        hub.handle_camera_hello(parsed)
        return _Outcome()
    raise ProtocolError(f"the camera may send only hello, ping and frames, not {parsed.type}")


def _try_dispatch(hub: Hub, settings: Settings, message: dict[str, Any]) -> _Outcome:
    try:
        return _dispatch(hub, settings, message)
    except ProtocolError as error:
        hub.note_rejected()
        log.warning("rejected camera message: %s", error)
        seq = error.seq if isinstance(error, FrameError) else None
        return _Outcome(error=error, ack=None if seq is None else hub.ack_frame(seq))


async def _serve_camera(
    ws: WebSocket,
    hub: Hub,
    settings: Settings,
    timeout_s: float,
    lease: int,
    superseded: asyncio.Event,
) -> None:
    errors: int | None = 0
    while errors is not None:
        event = await _next_phone_event(ws, superseded, timeout_s)
        # Re-checked with no await before the dispatch, so a replaced socket never applies.
        if not isinstance(event, _Close) and not hub.holds_camera(lease):
            event = REPLACED
        # Idle closes with 4408, as for the phone.
        if isinstance(event, _Close):
            log.info("closing camera connection: %s", event.reason)
            await _close_quietly(ws, event)
            return
        if event["type"] == DISCONNECT:
            return
        errors = await _reply(ws, _try_dispatch(hub, settings, event), errors, settings)


def build_camera_router(hub: Hub, settings: Settings, camera: CameraSettings) -> APIRouter:
    router = APIRouter()
    current: dict[str, asyncio.Event | None] = {"superseded": None}

    if camera.source == "external" and camera.token is None:
        log.warning("external camera chosen but DRIVE_CAMERA_TOKEN is not set: no frames")

    @router.websocket("/ws/camera")
    async def camera_socket(ws: WebSocket) -> None:
        reason = _refusal(ws.headers, camera)
        if reason is not None:
            log.warning("refused camera connection: %s", reason)
            await ws.close(code=CLOSE_POLICY_VIOLATION)
            return
        await ws.accept()
        # No await between superseding the old one and taking the slot.
        previous = current["superseded"]
        if previous is not None:
            previous.set()
        superseded = asyncio.Event()
        current["superseded"] = superseded
        lease = hub.attach_camera()
        log.info("camera connected (lease %d)", lease)
        try:
            await _serve_camera(ws, hub, settings, camera.idle_timeout_s, lease, superseded)
        except WebSocketDisconnect:
            pass
        finally:
            hub.detach_camera(lease)
            log.info("camera disconnected (lease %d)", lease)

    return router
