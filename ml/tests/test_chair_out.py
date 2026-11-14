"""ChairSender posts the model's commands to the driving service."""

import json

import httpx
import pytest

from fsd_ml.chair_out import TOKEN_ENV, ChairOutError, ChairSender, token_from_env

TOKEN = "made-up-driver-token-0123456789"
CHAIR = {"connected": True, "test": {"active": True, "source": "model"}}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _sender(handler, hold_ms: int = 600, clock=None) -> ChairSender:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ChairSender(
        "http://service:8000/", TOKEN, hold_ms=hold_ms, client=client, clock=clock or Clock()
    )


def test_send_posts_the_command_with_the_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "data": CHAIR, "error": None})

    result = _sender(handler, hold_ms=400).send(0.25, -0.1)
    assert result.sent and result.problem is None
    assert result.as_dict()["chair_connected"] is True
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://service:8000/api/chair/drive"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert "origin" not in request.headers
    assert json.loads(request.content) == {"v_mps": 0.25, "w_radps": -0.1, "hold_ms": 400}


def test_send_never_asks_for_reverse():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "data": CHAIR, "error": None})

    _sender(handler).send(-0.2, 0.0)
    assert bodies[0]["v_mps"] == 0.0


def test_a_refused_command_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        error = "chair is not connected or has not sent hello yet"
        return httpx.Response(409, json={"success": False, "data": None, "error": error})

    result = _sender(handler).send(0.2, 0.0)
    assert result.sent is False
    assert "409" in result.problem and "not connected" in result.problem


def test_an_unreachable_service_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = _sender(handler).send(0.2, 0.0)
    assert result.sent is False
    assert "cannot reach" in result.problem
    assert _sender(handler).stop().sent is False


def test_stop_deletes():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "data": CHAIR, "error": None})

    assert _sender(handler).stop().sent is True
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/api/chair/drive"


def test_stop_refused_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="nope")

    result = _sender(handler).stop()
    assert result.sent is False and "403" in result.problem


def test_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, f"  {TOKEN} ")
    assert token_from_env() == TOKEN
    monkeypatch.delenv(TOKEN_ENV)
    with pytest.raises(ChairOutError, match=TOKEN_ENV):
        token_from_env()


def test_a_sender_needs_a_token():
    with pytest.raises(ChairOutError):
        ChairSender("http://service:8000", "")


def test_sends_at_most_twenty_a_second_but_stops_at_once():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={"success": True, "data": CHAIR, "error": None})

    clock = Clock()
    sender = _sender(handler, clock=clock)
    first = sender.send(0.2, 0.0)
    clock.now += 0.02
    assert sender.send(0.3, 0.0) is first  # too soon: the service still holds the first
    clock.now += 0.04
    assert sender.send(0.3, 0.0).v_mps == 0.3
    clock.now += 0.01
    sender.stop()
    assert sender.send(0.1, 0.0).v_mps == 0.1  # after a stop, the next decision goes at once
    assert seen == ["POST", "POST", "DELETE", "POST"]


def test_reports_what_the_service_passes_on_when_it_lowers_a_command():
    def handler(request: httpx.Request) -> httpx.Response:
        data = {"connected": True, "test": {"v_mps": 0.5, "w_radps": -0.5}, "limited": True}
        return httpx.Response(200, json={"success": True, "data": data, "error": None})

    result = _sender(handler).send(1.2, -0.9)
    assert (result.v_mps, result.w_radps, result.limited) == (0.5, -0.5, True)
    assert result.as_dict()["limited"] is True
