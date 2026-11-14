"""Wiring for main.py: find the config, build the link, run it."""

import asyncio
import logging
import os
from pathlib import Path

from .client import ChairLink
from .config import ConfigError, load_config
from .mcu import BridgeCall, BridgeMcu, McuError

log = logging.getLogger(__name__)

CONFIG_ENV = "CHAIR_LINK_CONFIG"
CONFIG_FILE = "config.json"


def config_path(main_file: Path) -> Path:
    """config.json sits in the app folder, one level above python/main.py."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    return main_file.resolve().parent.parent / CONFIG_FILE


def build_link(path: Path, bridge_call: BridgeCall) -> ChairLink:
    """Builds the link. On a bad config the chair is told to stop before raising."""
    mcu = BridgeMcu(bridge_call)
    try:
        config = load_config(path)
    except ConfigError as exc:
        log.error("chair link not started: %s", exc)
        try:
            mcu.stop()
        except McuError as stop_exc:
            log.error("could not tell the microcontroller to stop: %s", stop_exc)
        raise
    log.info("chair link for %s (firmware %s)", config.service_url, config.firmware)
    return ChairLink(config, mcu)


def run_link(link: ChairLink) -> None:
    """Blocks running the link; used as App.run's user loop."""
    asyncio.run(link.run_forever())
