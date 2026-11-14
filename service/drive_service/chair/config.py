"""Chair link settings. Override any field with a ``DRIVE_CHAIR_``-prefixed environment variable.

The token may also come from ``service/.env`` (gitignored); copy ``chair.env.example``.
With no token the service refuses every chair connection (fail closed).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = SERVICE_ROOT / ".env"

MIN_TOKEN_CHARS = 16
# Example values are published in the repo, so a token containing one is never accepted.
PLACEHOLDER_MARKERS = ("replace-with", "change-me")
MIN_TTL_MS = 50
# The board may apply a command at most this late (judged by its reported applied_seq).
DEFAULT_COMMAND_LAG_MS = 500
CEILING_COMMAND_LAG_MS = 2000
# The board never holds a command longer than this, whatever it is told.
MAX_TTL_MS = 500
MS_PER_S = 1000.0
# A command must stay valid across this many intervals, so one late send never stops the chair.
TTL_INTERVALS = 2
# Hard ceilings for bench tests: a typo in an environment variable must not mean a fast chair.
CEILING_TEST_SPEED_MPS = 1.0
CEILING_TEST_TURN_RADPS = 1.0
CEILING_TEST_DURATION_MS = 10_000
CEILING_RATE_HZ = 50.0
# A model command is held at most this long before the service stops on its own.
CEILING_DRIVE_HOLD_MS = 1000


class ChairSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DRIVE_CHAIR_", env_file=ENV_FILE, extra="ignore", frozen=True
    )

    # Shared secret the board presents as "Authorization: Bearer <token>".
    token: SecretStr | None = None
    # Shared secret the model presents to POST /api/chair/drive. Unset refuses every model command.
    driver_token: SecretStr | None = None
    # How long one model command may be held. The model has to send again before then.
    max_drive_hold_ms: int = Field(default=600, ge=MIN_TTL_MS, le=CEILING_DRIVE_HOLD_MS)
    # Drive commands sent per second, stop commands included.
    rate_hz: float = Field(default=20.0, gt=0, le=CEILING_RATE_HZ)
    # How long the board may hold each command before stopping on its own.
    ttl_ms: int = Field(default=200, ge=MIN_TTL_MS, le=MAX_TTL_MS)
    # Bench test limits (POST /api/chair/test). The board also caps, independently.
    max_test_speed_mps: float = Field(default=0.5, gt=0, le=CEILING_TEST_SPEED_MPS)
    max_test_turn_radps: float = Field(default=0.5, gt=0, le=CEILING_TEST_TURN_RADPS)
    max_test_duration_ms: int = Field(default=3000, gt=0, le=CEILING_TEST_DURATION_MS)
    # A chair status older than this is shown as not fresh.
    status_stale_ms: float = Field(default=500.0, gt=0)
    # A board that sends nothing for this long is closed (4408) and the link cleared.
    idle_timeout_ms: int = Field(default=2000, ge=100, le=60_000)
    # Per-message size limit and the bad-message streak that closes the socket (1008).
    max_text_bytes: int = Field(default=2048, ge=256, le=65_536)
    max_consecutive_errors: int = Field(default=10, ge=1, le=1000)
    # A board whose applied_seq trails the newest command by more than this is closed (4409).
    max_command_lag_ms: int = Field(
        default=DEFAULT_COMMAND_LAG_MS, ge=MIN_TTL_MS, le=CEILING_COMMAND_LAG_MS
    )

    @field_validator("token", "driver_token")
    @classmethod
    def _check_token(cls, token: SecretStr | None) -> SecretStr | None:
        if token is None:
            return None
        value = token.get_secret_value().strip()
        if not value:
            return None
        if len(value) < MIN_TOKEN_CHARS:
            raise ValueError(f"chair token must be at least {MIN_TOKEN_CHARS} characters")
        lowered = value.lower()
        if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
            raise ValueError(
                "chair token is still the example placeholder; make a real one with "
                'python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        return SecretStr(value)

    @model_validator(mode="after")
    def _ttl_covers_intervals(self) -> ChairSettings:
        interval_ms = MS_PER_S / self.rate_hz
        if self.ttl_ms < TTL_INTERVALS * interval_ms:
            raise ValueError(
                f"ttl_ms {self.ttl_ms} must cover {TTL_INTERVALS} command intervals "
                f"({TTL_INTERVALS * interval_ms:g} ms at {self.rate_hz:g} Hz)"
            )
        if self.max_command_lag_ms < self.ttl_ms:
            raise ValueError(
                f"max_command_lag_ms {self.max_command_lag_ms} "
                f"must be at least ttl_ms {self.ttl_ms}"
            )
        if (
            self.token is not None
            and self.driver_token is not None
            and self.token.get_secret_value() == self.driver_token.get_secret_value()
        ):
            raise ValueError("the driver token must differ from the chair token")
        return self
