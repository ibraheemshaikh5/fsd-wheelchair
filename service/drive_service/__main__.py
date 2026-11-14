"""Run the driving service: ``uv run python -m drive_service`` from service/."""

from __future__ import annotations

import logging

import uvicorn

from drive_service.app import create_app
from drive_service.config import Settings

# Room for the frame header and WebSocket framing on top of the JPEG limit.
WS_OVERHEAD_BYTES = 4096
# Protocol-level pings find a dead link (phone powered off, VPN dropped) within
# about ten seconds even when the page cannot send a close frame.
WS_PING_INTERVAL_S = 5.0
WS_PING_TIMEOUT_S = 5.0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        ws_max_size=settings.max_frame_bytes + WS_OVERHEAD_BYTES,
        ws_ping_interval=WS_PING_INTERVAL_S,
        ws_ping_timeout=WS_PING_TIMEOUT_S,
    )


if __name__ == "__main__":
    main()
