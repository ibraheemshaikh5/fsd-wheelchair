"""Loads and validates config.json (service URL, token, firmware label)."""

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MIN_TOKEN_LENGTH = 16
DEFAULT_STATUS_HZ = 10.0
MIN_STATUS_HZ = 1.0
MAX_STATUS_HZ = 20.0
PLAIN_SCHEME = "ws"
ALLOWED_SCHEMES = (PLAIN_SCHEME, "wss")
# Plain ws:// is allowed only where the bytes never cross an untrusted network:
# loopback (e.g. through `adb reverse`) or Tailscale, which encrypts with WireGuard.
LOCALHOST = "localhost"
TAILNET_SUFFIX = ".ts.net"
TAILNET_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
PLACEHOLDER_MARKER = "CHANGE-ME"
EXAMPLE_FILE = "config.example.json"
# Must match MAX_FIRMWARE_CHARS in service/drive_service/chair/protocol.py.
MAX_FIRMWARE_CHARS = 64


class ConfigError(ValueError):
    """config.json is missing or invalid. The message says how to fix it."""


@dataclass(frozen=True)
class LinkConfig:
    service_url: str
    token: str = field(repr=False)
    firmware: str
    status_hz: float = DEFAULT_STATUS_HZ


def _require_str(data: dict[str, Any], key: str) -> str:
    if key not in data:
        raise ConfigError(f"config is missing '{key}'")
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{key}' must be a non-empty string")
    return value


def _is_private_link_host(host: str) -> bool:
    """True if plain ws:// to this host cannot be sniffed: loopback or the tailnet."""
    name = host.lower().rstrip(".")
    if name == LOCALHOST or name.endswith(TAILNET_SUFFIX):
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    return addr.is_loopback or any(addr in net for net in TAILNET_NETWORKS)


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ConfigError("'service_url' must start with ws:// or wss://")
    if not parsed.hostname:
        raise ConfigError("'service_url' has no host")
    if parsed.scheme == PLAIN_SCHEME and not _is_private_link_host(parsed.hostname):
        raise ConfigError(
            "'service_url' must use wss:// unless the host is loopback or on the tailnet "
            "(plain ws:// would send the token in cleartext)"
        )
    return url


def _validate_firmware(firmware: str) -> str:
    if len(firmware) > MAX_FIRMWARE_CHARS:
        raise ConfigError(f"'firmware' must be at most {MAX_FIRMWARE_CHARS} characters")
    return firmware


def _validate_token(token: str) -> str:
    if PLACEHOLDER_MARKER in token:
        raise ConfigError("'token' is still the placeholder; set it to DRIVE_CHAIR_TOKEN")
    if re.search(r"\s", token):
        raise ConfigError("'token' must not contain whitespace")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"'token' must be at least {MIN_TOKEN_LENGTH} characters")
    return token


def _validate_status_hz(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError("'status_hz' must be a number")
    if not MIN_STATUS_HZ <= value <= MAX_STATUS_HZ:
        raise ConfigError(f"'status_hz' must be between {MIN_STATUS_HZ} and {MAX_STATUS_HZ}")
    return float(value)


def parse_config(data: Any) -> LinkConfig:
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    return LinkConfig(
        service_url=_validate_url(_require_str(data, "service_url")),
        token=_validate_token(_require_str(data, "token")),
        firmware=_validate_firmware(_require_str(data, "firmware")),
        status_hz=_validate_status_hz(data.get("status_hz", DEFAULT_STATUS_HZ)),
    )


def load_config(path: Path) -> LinkConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path} not found: copy {EXAMPLE_FILE} to config.json and set the token"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    return parse_config(data)
