"""Assemble the driving service: hub, recorder, sockets, API, and the built web pages."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from drive_service import api, sockets
from drive_service.camera import CameraSettings, build_camera_router
from drive_service.chair import ChairSettings, build_chair
from drive_service.config import Settings
from drive_service.destination import DestinationStore
from drive_service.hub import Hub
from drive_service.origin import origin_allowed
from drive_service.ratelimit import RateWindow, admit
from drive_service.recorder import Recorder, limits_from_settings
from drive_service.timeutil import SystemTime, TimeSource

log = logging.getLogger(__name__)

API_PREFIX = "/api/"
# Requests that change something; they share a budget separate from status reads.
# Bench tests of the chair link (/api/chair/test) move motors: same budget and origin rule.
CONTROL_PREFIXES = ("/api/recording/", "/api/destination", "/api/goal", "/api/chair/")
# The model's drive commands arrive several times a second: their own budget.
DRIVE_PATH = "/api/chair/drive"


def _rate_budget(path: str, settings: Settings) -> tuple[str, int] | None:
    """Which rate window a request path counts against, as (state attribute, limit).

    Recording and destination changes have their own budget so a flood of status
    reads can never turn away a stop. Paths outside the API are not limited.
    """
    if path == DRIVE_PATH:
        return "drive_window", settings.max_drive_rate_hz
    if path.startswith(CONTROL_PREFIXES):
        return "control_window", settings.max_control_rate_hz
    if path.startswith(API_PREFIX):
        return "read_window", settings.max_api_rate_hz
    return None


def create_app(
    settings: Settings | None = None,
    chair_settings: ChairSettings | None = None,
    camera_settings: CameraSettings | None = None,
) -> FastAPI:
    settings = settings or Settings()
    chair_settings = chair_settings or ChairSettings()
    camera_settings = camera_settings or CameraSettings()
    time_source: TimeSource = SystemTime()
    recorder = Recorder(settings.record_dir, limits_from_settings(settings))
    external_camera = camera_settings.source == "external"
    hub = Hub(settings, recorder, time_source, external_camera=external_camera)
    log.info("camera frames come from %s", "/ws/camera" if external_camera else "the phone")
    destinations = DestinationStore()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await recorder.shutdown()

    app = FastAPI(title="FSD wheelchair driving service", lifespan=lifespan)
    app.state.read_window = RateWindow()
    app.state.control_window = RateWindow()
    app.state.drive_window = RateWindow()

    @app.middleware("http")
    async def limit_api_rate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        budget = _rate_budget(request.url.path, settings)
        if budget is not None:
            name, limit = budget
            # Refuse other sites before charging the control budget, or a page on
            # another site could spend it with no-cors POSTs and lock out a real Stop.
            # The handlers check the origin too.
            if name == "control_window" and not origin_allowed(
                request.headers, settings.allowed_origins
            ):
                return api.fail(403, api.FOREIGN_ORIGIN_MESSAGE)
            allowed, window = admit(getattr(app.state, name), time_source.mono(), limit)
            setattr(app.state, name, window)
            if not allowed:
                log.debug("rate limit hit on %s (%s per second)", request.url.path, limit)
                return api.fail(429, "too many requests")
        return await call_next(request)

    # Added last so it runs first: a request for a host name this service does not
    # own (DNS rebinding) is refused before it reaches the rate windows, the API,
    # the sockets or the pages. Covers HTTP and WebSocket.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    log.info("accepting requests for hosts: %s", ", ".join(settings.allowed_hosts))

    app.include_router(api.build_router(hub, recorder, destinations, settings, time_source))
    app.include_router(sockets.build_router(hub, recorder, destinations, settings, time_source))
    app.include_router(build_camera_router(hub, settings, camera_settings))
    chair = build_chair(chair_settings, settings.allowed_origins, time_source)
    app.include_router(chair.api_router)
    app.include_router(chair.ws_router)

    if settings.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dist, html=True), name="web")
    else:
        log.warning(
            "web pages not built (%s missing); run `npm run build` in web/", settings.web_dist
        )
    return app
