"""The Mac side of the chair link (docs/chair-link.md).

``build_chair`` returns the two routers, sharing one link state. Wire them into
the app with ``app.include_router`` for each.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter

from drive_service.chair.api import build_api_router
from drive_service.chair.config import ChairSettings
from drive_service.chair.drive_api import add_drive_routes
from drive_service.chair.link import ChairLinkStore
from drive_service.chair.sockets import build_ws_router
from drive_service.timeutil import SystemTime, TimeSource

log = logging.getLogger(__name__)

__all__ = ["Chair", "ChairSettings", "build_chair"]


@dataclass(frozen=True, slots=True)
class Chair:
    api_router: APIRouter
    ws_router: APIRouter
    store: ChairLinkStore


def build_chair(
    settings: ChairSettings,
    allowed_origins: tuple[str, ...],
    time_source: TimeSource | None = None,
) -> Chair:
    clock = time_source if time_source is not None else SystemTime()
    store = ChairLinkStore()
    if settings.token is None:
        log.warning("chair link disabled: DRIVE_CHAIR_TOKEN is not set, so /ws/chair refuses all")
    if settings.driver_token is None:
        log.info("model driving disabled: DRIVE_CHAIR_DRIVER_TOKEN is not set")
    api_router = build_api_router(store, settings, allowed_origins, clock)
    add_drive_routes(api_router, store, settings, clock)
    return Chair(
        api_router=api_router,
        ws_router=build_ws_router(store, settings, clock),
        store=store,
    )
