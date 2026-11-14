"""The model's drive route: POST/DELETE /api/chair/drive reach the chair socket."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from helpers import FakeTime
from test_chair_support import (
    AUTH,
    SAME_ORIGIN,
    TOKEN,
    as_json,
    chair_settings,
    hello_dict,
    make_app,
    status_dict,
)

# Made-up driver token used only by the tests.
DRIVER_TOKEN = "driver-test-token-0123456789"
DRIVER = {"authorization": f"Bearer {DRIVER_TOKEN}"}
GOOD = {"v_mps": 0.3, "w_radps": 0.2, "hold_ms": 500}


@pytest.fixture
def clock():
    return FakeTime()


@pytest.fixture
def client(clock):
    settings = chair_settings(driver_token=SecretStr(DRIVER_TOKEN))
    with TestClient(make_app(settings, time_source=clock)) as test_client:
        yield test_client


def _drive(ws) -> dict:
    return json.loads(ws.receive_text())


def _settle(chair, count: int = 2) -> dict:
    message = {}
    for _ in range(count):
        message = _drive(chair)
    return message


def _ready(client, **status):
    chair = client.websocket_connect("/ws/chair", headers=AUTH)
    session = chair.__enter__()
    session.send_text(as_json(hello_dict()))
    session.send_text(as_json(status_dict(applied_seq=None, **status)))
    _settle(session)
    return chair, session


def test_model_command_reaches_the_chair_and_then_expires(client, clock):
    chair_cm, chair = _ready(client)
    try:
        reply = client.post("/api/chair/drive", json=GOOD, headers=DRIVER)
        assert reply.status_code == 200
        assert reply.json()["data"]["test"]["source"] == "model"
        command = _settle(chair)
        assert command["type"] == "drive"
        assert (command["v_mps"], command["w_radps"]) == (0.3, 0.2)
        clock.advance(0.3)  # still within hold_ms
        assert _settle(chair)["v_mps"] == 0.3
        # A fresh status keeps the link ready; the hold then runs out on its own.
        chair.send_text(as_json(status_dict(applied_seq=None)))
        clock.advance(0.3)
        assert _settle(chair)["v_mps"] == 0.0
    finally:
        chair_cm.__exit__(None, None, None)


def test_a_newer_model_command_replaces_the_last(client):
    chair_cm, chair = _ready(client)
    try:
        client.post("/api/chair/drive", json=GOOD, headers=DRIVER)
        assert _settle(chair)["w_radps"] == 0.2
        client.post("/api/chair/drive", json={**GOOD, "w_radps": -0.1}, headers=DRIVER)
        assert _settle(chair)["w_radps"] == -0.1
    finally:
        chair_cm.__exit__(None, None, None)


def test_delete_stops_the_model_now(client):
    chair_cm, chair = _ready(client)
    try:
        client.post("/api/chair/drive", json=GOOD, headers=DRIVER)
        assert _settle(chair)["v_mps"] == 0.3
        reply = client.delete("/api/chair/drive", headers=DRIVER)
        assert reply.status_code == 200
        assert reply.json()["data"]["test"]["active"] is False
        assert _drive(chair)["v_mps"] == 0.0
    finally:
        chair_cm.__exit__(None, None, None)


def test_estop_report_cancels_the_model_command(client):
    chair_cm, chair = _ready(client)
    try:
        client.post("/api/chair/drive", json=GOOD, headers=DRIVER)
        assert _settle(chair)["v_mps"] == 0.3
        chair.send_text(as_json(status_dict(applied_seq=None, estop=True)))
        assert _settle(chair, 3)["v_mps"] == 0.0
        assert client.post("/api/chair/drive", json=GOOD, headers=DRIVER).status_code == 409
    finally:
        chair_cm.__exit__(None, None, None)


def test_model_needs_a_connected_chair(client):
    reply = client.post("/api/chair/drive", json=GOOD, headers=DRIVER)
    assert reply.status_code == 409
    assert "not connected" in reply.json()["error"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong-token-0123456789"},
        AUTH,  # the board's token is not the driver's
        {**DRIVER, **SAME_ORIGIN},  # no browser, not even the service's own page
    ],
)
def test_only_the_driver_token_without_an_origin_may_drive(client, headers):
    assert client.post("/api/chair/drive", json=GOOD, headers=headers).status_code == 403
    assert client.delete("/api/chair/drive", headers=headers).status_code == 403


def test_no_driver_token_configured_refuses_every_model_command():
    with TestClient(make_app(chair_settings(), time_source=FakeTime())) as client:
        reply = client.post("/api/chair/drive", json=GOOD, headers=DRIVER)
    assert reply.status_code == 403
    assert "DRIVE_CHAIR_DRIVER_TOKEN" in reply.json()["error"]


@pytest.mark.parametrize(
    "body",
    [
        {**GOOD, "v_mps": -0.1},
        {**GOOD, "v_mps": 5.1},
        {**GOOD, "w_radps": -5.1},
        {**GOOD, "hold_ms": 601},
        {**GOOD, "hold_ms": 0},
        {**GOOD, "v_mps": "0.3"},
        {**GOOD, "extra": 1},
        {"v_mps": 0.3, "w_radps": 0.0},
    ],
)
def test_out_of_range_model_commands_are_refused(client, body):
    chair_cm, _ = _ready(client)
    try:
        assert client.post("/api/chair/drive", json=body, headers=DRIVER).status_code == 422
        assert client.get("/api/chair").json()["data"]["test"]["active"] is False
    finally:
        chair_cm.__exit__(None, None, None)


def test_a_model_over_the_caps_is_lowered_to_them(client):
    chair_cm, chair = _ready(client)
    try:
        reply = client.post(
            "/api/chair/drive", json={**GOOD, "v_mps": 1.2, "w_radps": -0.9}, headers=DRIVER
        )
        assert reply.status_code == 200
        assert reply.json()["data"]["limited"] is True
        command = _settle(chair)
        assert (command["v_mps"], command["w_radps"]) == (0.5, -0.5)
        within = client.post("/api/chair/drive", json=GOOD, headers=DRIVER)
        assert within.json()["data"]["limited"] is False
    finally:
        chair_cm.__exit__(None, None, None)


def test_bad_json_is_refused(client):
    headers = {**DRIVER, "content-type": "application/json"}
    assert client.post("/api/chair/drive", content=b"{nope", headers=headers).status_code == 400


def test_driver_token_must_differ_from_the_chair_token():
    with pytest.raises(ValidationError, match="differ"):
        chair_settings(driver_token=SecretStr(TOKEN))


def test_driver_token_must_be_long_enough():
    with pytest.raises(ValidationError, match="at least"):
        chair_settings(driver_token=SecretStr("short"))
