"""Assemble the driving service: hub, recorder, sockets, API, and the built web pages."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from drive_service import api, sockets
from drive_service.camera import CameraSettings, build_camera_router
from drive_service.config import Settings
from drive_service.destination import DestinationStore
from drive_service.hub import Hub
from drive_service.recorder import Recorder
from drive_service.timeutil import SystemTime, TimeSource

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    camera_settings: CameraSettings | None = None,
) -> FastAPI:
    settings = settings or Settings()
    camera_settings = camera_settings or CameraSettings()
    time_source: TimeSource = SystemTime()
    recorder = Recorder(settings.record_dir)
    external_camera = camera_settings.source == "external"
    hub = Hub(recorder, time_source, external_camera=external_camera)
    log.info("camera frames come from %s", "/ws/camera" if external_camera else "the phone")
    destinations = DestinationStore()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await recorder.stop()

    app = FastAPI(title="FSD wheelchair driving service", lifespan=lifespan)
    # A request for a host name this service does not own (DNS rebinding) is refused
    # before it reaches the API, the sockets or the pages. Covers HTTP and WebSocket.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    log.info("accepting requests for hosts: %s", ", ".join(settings.allowed_hosts))

    app.include_router(api.build_router(hub, recorder, destinations, settings, time_source))
    app.include_router(sockets.build_router(hub, recorder, destinations, settings, time_source))
    app.include_router(build_camera_router(hub, camera_settings))

    if settings.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dist, html=True), name="web")
    else:
        log.warning(
            "web pages not built (%s missing); run `npm run build` in web/", settings.web_dist
        )
    return app
