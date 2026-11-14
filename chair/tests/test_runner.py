import json
from pathlib import Path

import pytest

from chair_link.client import ChairLink
from chair_link.config import ConfigError
from chair_link.runner import CONFIG_ENV, build_link, config_path


def test_config_path_is_next_to_the_python_folder(monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    main_file = Path("/app/python/main.py")
    assert config_path(main_file) == Path("/app/config.json")


def test_config_path_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "other.json"))
    assert config_path(Path("/app/python/main.py")) == tmp_path / "other.json"


def write_config(tmp_path: Path, token: str) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {"service_url": "ws://127.0.0.1:8000/ws/chair", "token": token, "firmware": "fw"}
        )
    )
    return path


def test_build_link_wires_bridge_call(tmp_path):
    calls = []
    link = build_link(write_config(tmp_path, "k" * 32), lambda m, *a, timeout: calls.append(m))
    assert isinstance(link, ChairLink)


def test_build_link_stops_chair_and_raises_on_bad_config(tmp_path):
    calls = []
    with pytest.raises(ConfigError):
        build_link(write_config(tmp_path, "short"), lambda m, *a, timeout: calls.append(m))
    assert calls == ["stop"]


def test_build_link_still_raises_config_error_when_stop_fails(tmp_path):
    def broken(method, *args, timeout):
        raise RuntimeError("router down")

    with pytest.raises(ConfigError):
        build_link(write_config(tmp_path, "short"), broken)
