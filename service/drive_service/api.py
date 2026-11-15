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
from drive_service.destination import (
    DestinationStore,
    goal_detail,
    parse_destination,
    parse_goal,
)
from drive_service.hub import Hub
from drive_service.origin import origin_allowed
from drive_service.protocol import CameraInfo
from drive_service.recorder import Recorder, RecorderBusyError, RecorderError
from drive_service.state import SensorSnapshot
from drive_service.status import live_status
from drive_service.timeutil import TimeSource

log = logging.getLogger(__name__)

NO_HELLO_MESSAGE = "phone is not connected or has not sent hello yet"
FOREIGN_ORIGIN_MESSAGE = "request came from another site"
BAD_JSON_MESSAGE = 'body must be JSON like {"lat": 40.35, "lon": -74.66}'
BAD_DESTINATION_MESSAGE = (
    "destination must be lat in [-90, 90] and lon in [-180, 180], nothing else"
)
# A destination body is a few dozen bytes; anything much bigger is not one.
MAX_DESTINATION_BODY_BYTES = 1024
BAD_GOAL_JSON_MESSAGE = (
    'body must be JSON like {"kind": "instruction", "text": "go to the kitchen"}'
    ' or {"kind": "route", "points": [{"lat": 40.35, "lon": -74.66}, ...]}'
)
BAD_GOAL_MESSAGE = (
    "goal must be a route of 2 to 2000 lat/lon points, or an instruction of 1 to 200 characters"
)
# 2000 points at about 50 bytes each, with room to spare.
MAX_GOAL_BODY_BYTES = 256 * 1024
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_CONFLICT = 409
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_UNPROCESSABLE = 422
HTTP_SERVER_ERROR = 500


def ok(data: Any) -> JSONResponse:
    return JSONResponse({"success": True, "data": data, "error": None})


def fail(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "data": None, "error": message}, status_code=status_code)


def _recorded_camera(snapshot: SensorSnapshot, external_camera: bool) -> CameraInfo | None:
    """The camera whose frames a recording holds: the external one, or the phone's."""
    hello = snapshot.camera_hello if external_camera else snapshot.hello
    return hello.camera if hello else None


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
        return ok(live_status(hub, recorder, destinations, time_source.mono()))

    @router.post("/recording/start")
    async def start_recording(request: Request) -> JSONResponse:
        if not origin_allowed(request.headers, settings.allowed_origins):
            return fail(HTTP_FORBIDDEN, FOREIGN_ORIGIN_MESSAGE)
        snapshot = hub.snapshot
        hello = snapshot.hello
        # Never record with missing metadata, or metadata left over from another phone.
        if not snapshot.phone_connected or hello is None:
            return fail(HTTP_CONFLICT, NO_HELLO_MESSAGE)
        camera = _recorded_camera(snapshot, hub.external_camera)
        meta = {
            "camera": camera.model_dump() if camera else None,
            "frame_source": "external" if hub.external_camera else "phone",
            "camera_clock": asdict(snapshot.camera_clock) if snapshot.camera_clock else None,
            "mount": hello.mount.model_dump() if hello.mount else None,
            "user_agent": hello.user_agent,
            "clock": asdict(snapshot.clock) if snapshot.clock else None,
        }
        try:
            return ok(asdict(recorder.start(meta)))
        except RecorderError as error:
            log.warning("could not start recording: %s", error)
            busy = isinstance(error, RecorderBusyError)
            return fail(HTTP_CONFLICT if busy else HTTP_SERVER_ERROR, str(error))

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
        return ok(live_status(hub, recorder, destinations, time_source.mono())["destination"])

    @router.get("/goal")
    async def get_goal() -> JSONResponse:
        return ok(goal_detail(destinations.goal))

    @router.post("/goal")
    async def set_goal(request: Request) -> JSONResponse:
        if not origin_allowed(request.headers, settings.allowed_origins):
            return fail(HTTP_FORBIDDEN, FOREIGN_ORIGIN_MESSAGE)
        raw = await request.body()
        if len(raw) > MAX_GOAL_BODY_BYTES:
            return fail(HTTP_PAYLOAD_TOO_LARGE, BAD_GOAL_JSON_MESSAGE)
        try:
            body = json.loads(raw)
        except ValueError:
            return fail(HTTP_BAD_REQUEST, BAD_GOAL_JSON_MESSAGE)
        try:
            wanted = parse_goal(body)
        except ValidationError as error:
            log.info("refused goal: %d validation errors", error.error_count())
            return fail(HTTP_UNPROCESSABLE, BAD_GOAL_MESSAGE)
        if isinstance(wanted, str):
            goal = destinations.set_instruction(wanted)
        else:
            goal = destinations.set_route(wanted)
        # Never log the words or the points: they say where the rider is going.
        log.info("goal set: %s", goal.kind)
        return ok(goal_detail(goal))

    @router.delete("/destination")
    async def clear_destination(request: Request) -> JSONResponse:
        if not origin_allowed(request.headers, settings.allowed_origins):
            return fail(HTTP_FORBIDDEN, FOREIGN_ORIGIN_MESSAGE)
        destinations.clear()
        log.info("destination cleared")
        return ok(None)

    return router
