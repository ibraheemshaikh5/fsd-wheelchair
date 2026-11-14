"""Chair HTTP API: link status and short bench test commands.

GET    /api/chair        link status
POST   /api/chair/test   {v_mps, w_radps, duration_ms}: drive for a moment, within the caps
DELETE /api/chair/test   stop now
POST   /api/chair/drive  the model's latest command (see drive_api.py)
DELETE /api/chair/drive  the model stops

The test routes accept only the service's own pages (or clients that send no
Origin, such as curl). Out-of-range commands are refused with 422; the service
never trims a request to fit, so what was asked is what runs or nothing does.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from drive_service.api import FOREIGN_ORIGIN_MESSAGE, fail, ok
from drive_service.chair.body import BadContentLength, BodyTooLarge, read_limited
from drive_service.chair.commander import BenchCommand, Caps, effective_caps
from drive_service.chair.config import MS_PER_S, ChairSettings
from drive_service.chair.link import (
    ChairLink,
    ChairLinkStore,
    chair_view,
    status_age_ms,
    status_fresh,
)
from drive_service.origin import origin_allowed
from drive_service.timeutil import TimeSource

log = logging.getLogger(__name__)

HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_CONFLICT = 409
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_UNPROCESSABLE = 422
MAX_TEST_BODY_BYTES = 1024
BAD_JSON_MESSAGE = 'body must be JSON like {"v_mps": 0.2, "w_radps": 0.0, "duration_ms": 1000}'
NOT_CONNECTED_MESSAGE = "chair is not connected or has not sent hello yet"
NO_STATUS_MESSAGE = "chair has not reported its e-stop and joystick state yet"
STALE_STATUS_MESSAGE = "its reports are not getting through"
ESTOP_MESSAGE = "chair reports the e-stop pressed or its wire open; release or jumper it first"
MANUAL_MESSAGE = "chair reports the rider on the joystick (manual mode)"


class BenchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    v_mps: float = Field(ge=0)
    w_radps: float
    duration_ms: int = Field(gt=0)


def motion_cap_error(v_mps: float, w_radps: float, caps: Caps) -> str | None:
    if v_mps > caps.max_speed_mps:
        return f"v_mps must be at most {caps.max_speed_mps:g}"
    if abs(w_radps) > caps.max_turn_radps:
        return f"|w_radps| must be at most {caps.max_turn_radps:g}"
    return None


def _cap_error(request: BenchRequest, caps: Caps, max_duration_ms: int) -> str | None:
    motion = motion_cap_error(request.v_mps, request.w_radps, caps)
    if motion is not None:
        return motion
    if request.duration_ms > max_duration_ms:
        return f"duration_ms must be at most {max_duration_ms}"
    return None


def not_ready(link: ChairLink, now_mono: float, stale_ms: float) -> str | None:
    """Why a bench test may not start now, or None. Needs a fresh, all-clear status."""
    if not link.connected or link.hello is None:
        return NOT_CONNECTED_MESSAGE
    if link.status is None:
        return NO_STATUS_MESSAGE
    if not status_fresh(link, now_mono, stale_ms):
        return f"chair status is {status_age_ms(link, now_mono)} ms old; {STALE_STATUS_MESSAGE}"
    status = link.status.status
    if status.estop:
        return ESTOP_MESSAGE
    if status.joystick_active or status.mode == "manual":
        return MANUAL_MESSAGE
    return None


async def read_request[Body: BaseModel](
    request: Request, model: type[Body], example: str
) -> Body | JSONResponse:
    """The body as ``model``, or the error response to send instead."""
    try:
        raw = await read_limited(request, MAX_TEST_BODY_BYTES)
    except BodyTooLarge as error:
        log.info("refused chair request: %s", error)
        return fail(HTTP_PAYLOAD_TOO_LARGE, example)
    except BadContentLength as error:
        log.info("refused chair request: %s", error)
        return fail(HTTP_BAD_REQUEST, example)
    try:
        json.loads(raw)
    except ValueError:
        return fail(HTTP_BAD_REQUEST, example)
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        log.info("refused chair request: %d validation errors", error.error_count())
        return fail(HTTP_UNPROCESSABLE, example)


def build_api_router(
    store: ChairLinkStore,
    settings: ChairSettings,
    allowed_origins: tuple[str, ...],
    clock: TimeSource,
) -> APIRouter:
    router = APIRouter(prefix="/api/chair")

    def view() -> JSONResponse:
        return ok(chair_view(store.state, clock.mono(), settings.status_stale_ms))

    @router.get("")
    async def chair_status() -> JSONResponse:
        return view()

    @router.post("/test")
    async def start_test(request: Request) -> JSONResponse:
        if not origin_allowed(request.headers, allowed_origins):
            return fail(HTTP_FORBIDDEN, FOREIGN_ORIGIN_MESSAGE)
        parsed = await read_request(request, BenchRequest, BAD_JSON_MESSAGE)
        if isinstance(parsed, JSONResponse):
            return parsed
        link = store.state
        problem = not_ready(link, clock.mono(), settings.status_stale_ms)
        if problem is not None:
            return fail(HTTP_CONFLICT, problem)
        caps = effective_caps(settings, link.hello)
        cap_error = _cap_error(parsed, caps, settings.max_test_duration_ms)
        if cap_error is not None:
            return fail(HTTP_UNPROCESSABLE, cap_error)
        bench = BenchCommand(
            v_mps=parsed.v_mps,
            w_radps=parsed.w_radps,
            expires_mono=clock.mono() + parsed.duration_ms / MS_PER_S,
        )
        store.update(lambda current: current.with_bench(bench))
        log.info(
            "bench test: %.2f m/s, %.2f rad/s for %d ms",
            parsed.v_mps,
            parsed.w_radps,
            parsed.duration_ms,
        )
        return view()

    @router.delete("/test")
    async def stop_test(request: Request) -> JSONResponse:
        if not origin_allowed(request.headers, allowed_origins):
            return fail(HTTP_FORBIDDEN, FOREIGN_ORIGIN_MESSAGE)
        store.update(lambda current: current.without_bench())
        log.info("bench test stopped by request")
        return view()

    return router
