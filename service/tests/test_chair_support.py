"""Shared builders for the chair link tests. Holds no tests of its own."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from pydantic import SecretStr

from drive_service.chair import build_chair
from drive_service.chair.config import ChairSettings
from drive_service.timeutil import SystemTime, TimeSource

# A made-up token used only by the tests.
TOKEN = "test-token-0123456789abcdef"
AUTH = {"authorization": f"Bearer {TOKEN}"}
HOST = "testserver"
SAME_ORIGIN = {"origin": f"http://{HOST}"}
OTHER_ORIGIN = {"origin": "https://evil.example"}


def chair_settings(**overrides: Any) -> ChairSettings:
    """Settings that never read the developer's .env file."""
    base: dict[str, Any] = {"token": SecretStr(TOKEN)}
    return ChairSettings(_env_file=None, **{**base, **overrides})


def make_app(
    settings: ChairSettings | None = None,
    time_source: TimeSource | None = None,
    allowed_origins: tuple[str, ...] = (),
) -> FastAPI:
    """A minimal app holding only the chair routers."""
    chair = build_chair(
        settings if settings is not None else chair_settings(),
        allowed_origins,
        time_source if time_source is not None else SystemTime(),
    )
    app = FastAPI()
    app.include_router(chair.api_router)
    app.include_router(chair.ws_router)
    return app


def hello_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "type": "hello",
        "role": "chair",
        "firmware": "test-1.0",
        "max_speed_mps": 0.5,
        "max_turn_radps": 0.5,
    }
    return {**base, **overrides}


def status_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "type": "chair",
        "t": 1_794_650_000_140,
        "mode": "auto",
        "joystick_active": False,
        "estop": False,
        "battery_pct": 82,
        "applied_seq": 3,
        "output": {"v_mps": 0.0, "w_radps": 0.0},
        "stopped_reason": None,
    }
    return {**base, **overrides}


def as_json(message: dict[str, Any]) -> str:
    return json.dumps(message)
