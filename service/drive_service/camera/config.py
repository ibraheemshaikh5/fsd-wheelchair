"""External camera settings. Override any field with a ``DRIVE_CAMERA_``-prefixed variable.

The token may also come from ``service/.env`` (gitignored); copy ``camera.env.example``.
With no token the service refuses every camera connection (fail closed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
MIN_TOKEN_CHARS = 16
# Example values are published in the repo, so a token containing one is never accepted.
PLACEHOLDER_MARKERS = ("replace-with", "change-me")

# "phone": frames come from the phone's capture page, as before.
# "external": frames come from a camera sender on /ws/camera (a USB camera read by
# scripts/camera_sender.py); the phone or iPad sends only GPS, compass and motion.
type FrameSource = Literal["phone", "external"]


class CameraSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DRIVE_CAMERA_", env_file=ENV_FILE, extra="ignore", frozen=True
    )

    source: FrameSource = "phone"
    # Shared secret the camera sender presents as "Authorization: Bearer <token>".
    token: SecretStr | None = None
    # A camera socket that sends nothing for this long is closed (4408). The sender
    # streams about 20 frames a second and pings every second.
    idle_timeout_s: float = Field(default=5.0, gt=0, le=60)

    @field_validator("token")
    @classmethod
    def _check_token(cls, token: SecretStr | None) -> SecretStr | None:
        if token is None:
            return None
        value = token.get_secret_value().strip()
        if not value:
            return None
        if len(value) < MIN_TOKEN_CHARS:
            raise ValueError(f"camera token must be at least {MIN_TOKEN_CHARS} characters")
        if any(marker in value.lower() for marker in PLACEHOLDER_MARKERS):
            raise ValueError(
                "camera token is still the example placeholder; make a real one with "
                'python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        return SecretStr(value)
