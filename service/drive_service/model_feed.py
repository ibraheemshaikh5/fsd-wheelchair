"""The driving model's feed (/ws/model): every camera frame, sent the moment it lands.

The viewer feed (/ws/viewer) wakes on a timer and sends whatever frame is
newest, which suits a screen. The model needs each frame as soon as it exists,
so this feed waits on the hub's frame signal instead: a frame is on its way to
the model within the same event-loop turn it was stored.

Each new frame goes out as the status (text) and then the frame (binary), built
with no await in between so the status describes that frame. The binary form is
the phone's, ``[uint32 LE header length][header JSON][JPEG]``, with one more
header field, ``age_ms``: how long ago the phone captured the frame, on this
service's clock, at the moment it was sent (null before the phone's clock is
synced). A model on another machine subtracts it from its own clock to place
the frame in time, so the two clocks never need to agree. With no new frame the
status still goes out every ``interval_s``, so a model learns at once that the
camera went stale. Anything the model sends is ignored.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket

from drive_service.hub import Hub
from drive_service.protocol import Frame
from drive_service.state import MS_PER_S, Stamped

DISCONNECT = "websocket.disconnect"
_LENGTH_PREFIX = struct.Struct("<I")

type StatusNow = Callable[[], dict[str, Any]]
type MonoNow = Callable[[], float]


def model_frame_bytes(stamped: Stamped[Frame], now_mono: float) -> bytes:
    """A stored frame in the feed's binary form, aged as of ``now_mono``."""
    header = stamped.value.header
    fields: dict[str, Any] = {"seq": header.seq, "t": header.t, "w": header.w, "h": header.h}
    if header.rot is not None:
        fields["rot"] = header.rot
    captured = stamped.captured_mono
    fields["age_ms"] = None if captured is None else round((now_mono - captured) * MS_PER_S, 1)
    encoded = json.dumps(fields).encode()
    return _LENGTH_PREFIX.pack(len(encoded)) + encoded + stamped.value.jpeg


def _cancel(task: asyncio.Future[Any]) -> None:
    """Drop a wait we no longer need, retrieving any error it already raised."""
    if not task.done():
        task.cancel()
    elif not task.cancelled():
        task.exception()


class _Listener:
    """Reads (and ignores) what the model sends, to notice when it leaves."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self.task: asyncio.Future[Any] = asyncio.ensure_future(ws.receive())

    def gone(self) -> bool:
        """After the task finished: True if the model left, else listen again."""
        if self.task.result()["type"] == DISCONNECT:
            return True
        self.task = asyncio.ensure_future(self._ws.receive())
        return False

    def close(self) -> None:
        _cancel(self.task)


async def _send_update(
    ws: WebSocket,
    hub: Hub,
    status_now: StatusNow,
    now_mono: MonoNow,
    last_sent: Stamped[Frame] | None,
) -> Stamped[Frame] | None:
    """Send the status, then the newest frame if it is new. Returns what was last sent."""
    # No await between these lines, so the frame and its age match the status.
    status = status_now()
    frame = hub.snapshot.frame
    payload = None if frame is None or frame is last_sent else model_frame_bytes(frame, now_mono())
    await ws.send_text(json.dumps(status))
    if payload is None:
        return last_sent
    await ws.send_bytes(payload)
    return frame


async def _wait(listener: _Listener, arrived: asyncio.Event, interval_s: float) -> bool:
    """Wait for a new frame or the next status tick. False once the model has left."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + interval_s
    wake = asyncio.ensure_future(arrived.wait())
    try:
        while (remaining := deadline - loop.time()) > 0:
            done, _ = await asyncio.wait(
                {listener.task, wake}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if wake in done or not done:
                return True
            # The model said something: ignore it, but never let it speed up the feed.
            if listener.gone():
                return False
        return True
    finally:
        _cancel(wake)


async def serve_model(
    ws: WebSocket, hub: Hub, status_now: StatusNow, now_mono: MonoNow, interval_s: float
) -> None:
    """Feed one model connection until it leaves."""
    listener = _Listener(ws)
    last_sent: Stamped[Frame] | None = None
    try:
        while True:
            # Taken before sending, so a frame that lands while sending is not missed.
            arrived = hub.frame_arrived()
            last_sent = await _send_update(ws, hub, status_now, now_mono, last_sent)
            if not await _wait(listener, arrived, interval_s):
                return
    finally:
        listener.close()
