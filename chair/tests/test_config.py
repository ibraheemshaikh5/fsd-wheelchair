import json
from pathlib import Path

import pytest

from chair_link.config import (
    MAX_FIRMWARE_CHARS,
    ConfigError,
    LinkConfig,
    load_config,
    parse_config,
)

GOOD_TOKEN = "a" * 32


def raw(**overrides):
    base = {
        "service_url": "wss://wheelchair-mac.example.ts.net/ws/chair",
        "token": GOOD_TOKEN,
        "firmware": "chair-link 0.1.0",
    }
    return {**base, **overrides}


def test_parses_valid_config_with_defaults():
    cfg = parse_config(raw())
    assert cfg == LinkConfig(
        service_url="wss://wheelchair-mac.example.ts.net/ws/chair",
        token=GOOD_TOKEN,
        firmware="chair-link 0.1.0",
        status_hz=10.0,
    )


def test_config_is_frozen():
    cfg = parse_config(raw())
    with pytest.raises(AttributeError):
        cfg.token = "x"  # type: ignore[misc]


def test_token_is_hidden_from_repr():
    assert GOOD_TOKEN not in repr(parse_config(raw()))


@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.1:8000/ws/chair",
        "ws://localhost:8000/ws/chair",
        "ws://[::1]:8000/ws/chair",
        "ws://100.101.102.103:8000/ws/chair",
        "ws://[fd7a:115c:a1e0::1]:8000/ws/chair",
        "ws://wheelchair-mac.taile75b1b.ts.net/ws/chair",
        "wss://192.168.1.20/ws/chair",
    ],
)
def test_accepts_plain_ws_only_on_loopback_or_tailnet(url):
    assert parse_config(raw(service_url=url)).service_url == url


@pytest.mark.parametrize(
    "url",
    [
        "ws://192.168.1.20:8000/ws/chair",
        "ws://wheelchair-mac.local:8000/ws/chair",
        "ws://example.com/ws/chair",
        "ws://evil.ts.net.example.com/ws/chair",
        "ws://100.128.0.1:8000/ws/chair",
        "ws://10.0.0.5:8000/ws/chair",
    ],
)
def test_rejects_plain_ws_that_would_leak_the_token(url):
    with pytest.raises(ConfigError, match="wss://"):
        parse_config(raw(service_url=url))


def test_firmware_length_matches_the_service_limit():
    assert parse_config(raw(firmware="f" * MAX_FIRMWARE_CHARS)).firmware == "f" * 64
    with pytest.raises(ConfigError, match="64"):
        parse_config(raw(firmware="f" * (MAX_FIRMWARE_CHARS + 1)))


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"service_url": "https://mac/ws/chair"}, "ws:// or wss://"),
        ({"service_url": "wss:///ws/chair"}, "host"),
        ({"service_url": 5}, "service_url"),
        ({"token": "short"}, "at least 16"),
        ({"token": "has space in it 12345"}, "whitespace"),
        ({"token": "CHANGE-ME-to-a-long-random-token"}, "placeholder"),
        ({"firmware": ""}, "firmware"),
        ({"status_hz": 0}, "status_hz"),
        ({"status_hz": 50}, "status_hz"),
        ({"status_hz": True}, "status_hz"),
    ],
)
def test_rejects_bad_values(overrides, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw(**overrides))


def test_rejects_missing_keys():
    data = raw()
    del data["token"]
    with pytest.raises(ConfigError, match="token"):
        parse_config(data)


def test_rejects_non_object():
    with pytest.raises(ConfigError, match="object"):
        parse_config(["not", "an", "object"])


def test_load_config_reads_file(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw()))
    assert load_config(path).token == GOOD_TOKEN


def test_load_config_explains_missing_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="config.example.json"):
        load_config(tmp_path / "config.json")


def test_load_config_explains_bad_json(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(path)


def test_shipped_example_has_the_same_keys_but_a_placeholder_token():
    example = Path(__file__).resolve().parents[1] / "unoq-app" / "config.example.json"
    data = json.loads(example.read_text())
    assert set(data) >= {"service_url", "token", "firmware"}
    with pytest.raises(ConfigError, match="placeholder"):
        parse_config(data)
