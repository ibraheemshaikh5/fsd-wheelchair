"""Priority phones: a listed Tailscale user keeps the phone slot against everyone else."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from drive_service.app import create_app
from drive_service.config import Settings
from helpers import as_json, gps_dict, ping_dict

HOST = "testserver"
OWNER = "owner@example.com"
FRIEND = "friend@github"
LOGIN_HEADER = "tailscale-user-login"
CLOSE_PHONE_BUSY = 4409
CLOSE_PHONE_REPLACED = 4410


def _as(login: str) -> dict[str, str]:
    return {LOGIN_HEADER: login}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        record_dir=tmp_path / "recordings",
        web_dist=tmp_path / "missing",
        allowed_hosts=(HOST,),
        priority_users=(OWNER.upper(),),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _pong(phone: WebSocketTestSession) -> None:
    phone.send_text(as_json(ping_dict()))
    assert json.loads(phone.receive_text())["type"] == "pong"


def _status(client: TestClient) -> dict:
    response = client.get("/api/status")
    assert response.status_code == 200
    return response.json()["data"]


def test_priority_users_are_lowercased(tmp_path):
    settings = Settings(record_dir=tmp_path, priority_users=("Owner@Example.com",))
    assert settings.priority_users == ("owner@example.com",)


def test_priority_users_default_to_none(tmp_path):
    assert Settings(record_dir=tmp_path).priority_users == ()


def test_owner_takes_over_from_friend(client):
    with client.websocket_connect("/ws/phone", headers=_as(FRIEND)) as friend:
        _pong(friend)
        with client.websocket_connect("/ws/phone", headers=_as(OWNER)) as owner:
            with pytest.raises(WebSocketDisconnect) as closed:
                friend.receive_text()
            assert closed.value.code == CLOSE_PHONE_REPLACED
            _pong(owner)


def test_friend_is_turned_away_while_owner_streams(client):
    with client.websocket_connect("/ws/phone", headers=_as(OWNER)) as owner:
        _pong(owner)
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect("/ws/phone", headers=_as(FRIEND)) as friend,
        ):
            friend.receive_text()
        assert closed.value.code == CLOSE_PHONE_BUSY
        owner.send_text(as_json(gps_dict(lat=12.5)))
        _pong(owner)
        status = _status(client)
        assert status["phone_connected"] is True
        assert status["gps"]["lat"] == 12.5


def test_unnamed_phone_is_turned_away_while_owner_streams(client):
    with client.websocket_connect("/ws/phone", headers=_as(OWNER)) as owner:
        _pong(owner)
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect("/ws/phone") as anonymous,
        ):
            anonymous.receive_text()
        assert closed.value.code == CLOSE_PHONE_BUSY
        _pong(owner)


def test_owner_reconnect_replaces_owner(client):
    with client.websocket_connect("/ws/phone", headers=_as(OWNER)) as first:
        _pong(first)
        with client.websocket_connect("/ws/phone", headers=_as(OWNER)) as second:
            with pytest.raises(WebSocketDisconnect) as closed:
                first.receive_text()
            assert closed.value.code == CLOSE_PHONE_REPLACED
            _pong(second)


def test_friend_can_stream_once_owner_leaves(client):
    with client.websocket_connect("/ws/phone", headers=_as(OWNER)) as owner:
        _pong(owner)
    assert _status(client)["phone_connected"] is False
    with client.websocket_connect("/ws/phone", headers=_as(FRIEND)) as friend:
        _pong(friend)
        assert _status(client)["phone_connected"] is True


def test_friends_replace_each_other_as_before(client):
    with client.websocket_connect("/ws/phone", headers=_as(FRIEND)) as first:
        _pong(first)
        with client.websocket_connect("/ws/phone", headers=_as("other@github")) as second:
            with pytest.raises(WebSocketDisconnect) as closed:
                first.receive_text()
            assert closed.value.code == CLOSE_PHONE_REPLACED
            _pong(second)
