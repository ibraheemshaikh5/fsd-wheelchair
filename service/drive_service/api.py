"""HTTP API: status and recording control. Every response uses one envelope."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from drive_service.config import Settings
from drive_service.destination import DestinationStore, parse_destination
from drive_service.hub import Hub
from drive_service.origin import origin_allowed
from drive_service.recorder import (
    InsufficientDiskError,
    Recorder,
    RecorderBusyError,
    RecorderError,
)
from drive_service.status import live_status
from drive_service.timeutil import TimeSource

log = logging.getLogger(__name__)

NO_HELLO_MESSAGE = "phone is not connected or has not sent hello yet"
FOREIGN_ORIGIN_MESSAGE = "request came from another site"
BAD_JSON_MESSAGE = 'body must be JSON like {"lat": 38.99, "lon": -76.94}'
BAD_DESTINATION_MESSAGE = (
    "destination must be lat in [-90, 90] and lon in [-180, 180], nothing else"
)
# A destination body is a few dozen bytes; anything much bigger is not one.
MAX_DESTINATION_BODY_BYTES = 1024
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_CONFLICT = 409
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_UNPROCESSABLE = 422
HTTP_SERVER_ERROR = 500
HTTP_INSUFFICIENT_STORAGE = 507


def ok(data: Any) -> JSONResponse:
    return JSONResponse({"success": True, "data": data, "error": None})


def fail(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "data": None, "error": message}, status_code=status_code)


def build_router(
    hub: Hub,
    recorder: Recorder,
    destinations: DestinationStore,
    settings: Settings,
    time_source: TimeSource,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/status")
    async def status() -> JSONResponse:
        return ok(live_status(hub, recorder, destinations, time_source.mono(), settings))

    @router.post("/recording/start")
    async def start_recording(request: Request) -> JSONResponse:
        if not origin_allowed(request.headers, settings.allowed_origins):
            return fail(HTTP_FORBIDDEN, FOREIGN_ORIGIN_MESSAGE)
        snapshot = hub.snapshot
        hello = snapshot.hello
        # Never record with missing metadata, or metadata left over from another phone.
        if not snapshot.phone_connected or hello is None:
            return fail(HTTP_CONFLICT, NO_HELLO_MESSAGE)
        meta = {
            "camera": hello.camera.model_dump(),
            "mount": hello.mount.model_dump() if hello.mount else None,
            "user_agent": hello.user_agent,
            "clock": asdict(snapshot.clock) if snapshot.clock else None,
        }
        try:
            return ok(asdict(recorder.start(meta)))
        except RecorderError as error:
            log.warning("could not start recording: %s", error)
            return fail(_start_error_status(error), str(error))

    @router.post("/recording/stop")
    async def stop_recording(request: Request) -> JSONResponse:
        if not origin_allowed(request.headers, settings.allowed_origins):
            return fail(HTTP_FORBIDDEN, FOREIGN_ORIGIN_MESSAGE)
        return ok(asdict(await recorder.stop()))

    @router.post("/destination")
    async def set_destination(request: Request) -> JSONResponse:
        if not origin_allowed(request.headers, settings.allowed_origins):
            return fail(HTTP_FORBIDDEN, FOREIGN_ORIGIN_MESSAGE)
        raw = await request.body()
        if len(raw) > MAX_DESTINATION_BODY_BYTES:
            return fail(HTTP_PAYLOAD_TOO_LARGE, BAD_JSON_MESSAGE)
        try:
            body = json.loads(raw)
        except ValueError:
            return fail(HTTP_BAD_REQUEST, BAD_JSON_MESSAGE)
        try:
            destination = parse_destination(body)
        except ValidationError as error:
            log.info("refused destination: %d validation errors", error.error_count())
            return fail(HTTP_UNPROCESSABLE, BAD_DESTINATION_MESSAGE)
        destinations.set(destination)
        # Never log the coordinates: they say where the rider is going.
        log.info("destination set")
        return ok(
            live_status(hub, recorder, destinations, time_source.mono(), settings)["destination"]
        )

    @router.delete("/destination")
    async def clear_destination(request: Request) -> JSONResponse:
        if not origin_allowed(request.headers, settings.allowed_origins):
            return fail(HTTP_FORBIDDEN, FOREIGN_ORIGIN_MESSAGE)
        destinations.clear()
        log.info("destination cleared")
        return ok(None)

    return router


def _start_error_status(error: RecorderError) -> int:
    if isinstance(error, RecorderBusyError):
        return HTTP_CONFLICT
    if isinstance(error, InsufficientDiskError):
        return HTTP_INSUFFICIENT_STORAGE
    return HTTP_SERVER_ERROR
