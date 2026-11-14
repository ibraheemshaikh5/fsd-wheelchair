"""Chair link settings: defaults, bounds and the token."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

import drive_service.chair
from drive_service.chair.config import ChairSettings
from test_chair_support import TOKEN

EXAMPLE_ENV = Path(drive_service.chair.__file__).parent / "chair.env.example"


def _settings(**overrides) -> ChairSettings:
    return ChairSettings(_env_file=None, **overrides)


def test_defaults_fail_closed_with_no_token(monkeypatch):
    monkeypatch.delenv("DRIVE_CHAIR_TOKEN", raising=False)
    settings = _settings()
    assert settings.token is None
    assert settings.rate_hz == 20
    assert settings.ttl_ms == 200
    assert settings.max_test_speed_mps == 0.5
    assert settings.max_test_turn_radps == 0.5
    assert settings.max_test_duration_ms == 3000
    assert settings.status_stale_ms == 500


def test_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("DRIVE_CHAIR_TOKEN", TOKEN)
    settings = _settings()
    assert settings.token is not None
    assert settings.token.get_secret_value() == TOKEN


def test_token_is_never_shown(monkeypatch):
    settings = _settings(token=SecretStr(TOKEN))
    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings.model_dump())


def test_blank_token_means_no_token():
    assert _settings(token=SecretStr("   ")).token is None


def test_short_token_is_refused():
    with pytest.raises(ValidationError, match="at least"):
        _settings(token=SecretStr("short"))


@pytest.mark.parametrize(
    "token",
    [
        "replace-with-a-long-random-token",
        "CHANGE-ME-to-a-long-random-token",
        "my-token-change-me-0123456789",
        "Replace-With-Something-Random-Please",
    ],
)
def test_placeholder_tokens_are_refused(token):
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(token=SecretStr(token))


def _example_token() -> str:
    for line in EXAMPLE_ENV.read_text().splitlines():
        if line.startswith("DRIVE_CHAIR_TOKEN="):
            return line.split("=", 1)[1]
    raise AssertionError("chair.env.example has no DRIVE_CHAIR_TOKEN line")


def test_the_example_files_token_is_refused(monkeypatch):
    monkeypatch.delenv("DRIVE_CHAIR_TOKEN", raising=False)
    assert _example_token(), "the example must show where the token goes"
    with pytest.raises(ValidationError, match="placeholder"):
        ChairSettings(_env_file=EXAMPLE_ENV)


def test_command_lag_limit_defaults_to_500_ms():
    assert _settings().max_command_lag_ms == 500


def test_command_lag_limit_must_cover_the_ttl():
    with pytest.raises(ValidationError, match="max_command_lag_ms"):
        _settings(ttl_ms=300, max_command_lag_ms=200)


@pytest.mark.parametrize("ttl_ms", [49, 501, 1000])
def test_ttl_must_be_between_50_and_500(ttl_ms):
    with pytest.raises(ValidationError):
        _settings(ttl_ms=ttl_ms)


def test_ttl_must_cover_two_command_intervals():
    # At 5 Hz a command comes every 200 ms; a 200 ms ttl would stop the chair between them.
    with pytest.raises(ValidationError, match="ttl_ms"):
        _settings(rate_hz=5, ttl_ms=200)


@pytest.mark.parametrize(
    "field", ["max_test_speed_mps", "max_test_turn_radps", "max_test_duration_ms"]
)
def test_test_caps_have_hard_ceilings(field):
    with pytest.raises(ValidationError):
        _settings(**{field: 1_000_000})


@pytest.mark.parametrize("field", ["max_test_speed_mps", "max_test_turn_radps", "rate_hz"])
def test_caps_and_rate_must_be_positive(field):
    with pytest.raises(ValidationError):
        _settings(**{field: 0})


def test_settings_are_frozen():
    settings = _settings()
    with pytest.raises(ValidationError):
        settings.ttl_ms = 300
