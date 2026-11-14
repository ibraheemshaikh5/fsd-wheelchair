"""The model's way to the chair.

POST   /api/chair/drive  {v_mps, w_radps, hold_ms}: the model's latest decision
DELETE /api/chair/drive  stop now

The model (``ml/fsd_ml/chair_out.py``) posts each decision here; the chair
socket sends it on, on every 20 Hz tick, until ``hold_ms`` runs out or a newer
decision replaces it. A model that stops posting therefore stops the chair.

Only a client holding ``DRIVE_CHAIR_DRIVER_TOKEN`` may drive, and never a
browser: any request with an ``Origin`` header is refused. Readiness is that of
a bench test (409 when the chair is not ready). Unlike a bench test, a model
asking for more than the caps is lowered to them (the response says
``limited``), since the model plans its own speed; values no chair could mean
are refused with 422. How the two numbers look on the wire to the board is decided
in ``protocol.DriveCommand``, not here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from drive_service.api import fail, ok
from drive_service.chair.api import (
    HTTP_CONFLICT,
    HTTP_FORBIDDEN,
    HTTP_UNPROCESSABLE,
    not_ready,
    read_request,
)
from drive_service.chair.commander import SOURCE_MODEL, BenchCommand, Caps, effective_caps
from drive_service.chair.config import MS_PER_S, ChairSettings
from drive_service.chair.link import ChairLinkStore, chair_view
from drive_service.chair.sockets import bearer_token_ok
from drive_service.timeutil import TimeSource

log = logging.getLogger(__name__)

DRIVE_EXAMPLE = 'body must be JSON like {"v_mps": 0.2, "w_radps": 0.0, "hold_ms": 500}'
NO_DRIVER_TOKEN_MESSAGE = "model driving is off: DRIVE_CHAIR_DRIVER_TOKEN is not set"
BAD_DRIVER_TOKEN_MESSAGE = "missing or wrong driver token"
BROWSER_MESSAGE = "a browser may not drive the chair"
# Beyond these a request is garbage, not a fast plan: refused rather than lowered.
SANE_SPEED_MPS = 5.0
SANE_TURN_RADPS = 5.0


class DriveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    v_mps: float = Field(ge=0, le=SANE_SPEED_MPS)
    w_radps: float = Field(ge=-SANE_TURN_RADPS, le=SANE_TURN_RADPS)
    hold_ms: int = Field(gt=0)


def within_caps(v_mps: float, w_radps: float, caps: Caps) -> tuple[float, float]:
    return (
        min(v_mps, caps.max_speed_mps),
        max(-caps.max_turn_radps, min(caps.max_turn_radps, w_radps)),
    )


def driver_refusal(headers: Mapping[str, str], settings: ChairSettings) -> str | None:
    """Why this request may not drive, or None."""
    if "origin" in headers:
        return BROWSER_MESSAGE
    if settings.driver_token is None:
        return NO_DRIVER_TOKEN_MESSAGE
    if not bearer_token_ok(headers, settings.driver_token):
        return BAD_DRIVER_TOKEN_MESSAGE
    return None


def add_drive_routes(
    router: APIRouter,
    store: ChairLinkStore,
    settings: ChairSettings,
    clock: TimeSource,
) -> None:
    def view() -> JSONResponse:
        return ok(chair_view(store.state, clock.mono(), settings.status_stale_ms))

    @router.post("/drive")
    async def drive(request: Request) -> JSONResponse:
        refusal = driver_refusal(request.headers, settings)
        if refusal is not None:
            return fail(HTTP_FORBIDDEN, refusal)
        parsed = await read_request(request, DriveRequest, DRIVE_EXAMPLE)
        if isinstance(parsed, JSONResponse):
            return parsed
        link = store.state
        problem = not_ready(link, clock.mono(), settings.status_stale_ms)
        if problem is not None:
            return fail(HTTP_CONFLICT, problem)
        if parsed.hold_ms > settings.max_drive_hold_ms:
            return fail(HTTP_UNPROCESSABLE, f"hold_ms must be at most {settings.max_drive_hold_ms}")
        v_mps, w_radps = within_caps(
            parsed.v_mps, parsed.w_radps, effective_caps(settings, link.hello)
        )
        command = BenchCommand(
            v_mps=v_mps,
            w_radps=w_radps,
            expires_mono=clock.mono() + parsed.hold_ms / MS_PER_S,
            source=SOURCE_MODEL,
        )
        store.update(lambda current: current.with_bench(command))
        log.debug("model drive: %.2f m/s, %.2f rad/s", v_mps, w_radps)
        limited = (v_mps, w_radps) != (parsed.v_mps, parsed.w_radps)
        return ok(
            {**chair_view(store.state, clock.mono(), settings.status_stale_ms), "limited": limited}
        )

    @router.delete("/drive")
    async def stop(request: Request) -> JSONResponse:
        refusal = driver_refusal(request.headers, settings)
        if refusal is not None:
            return fail(HTTP_FORBIDDEN, refusal)
        store.update(lambda current: current.without_bench())
        log.debug("model drive stopped by request")
        return view()
