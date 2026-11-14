"""The fake chair's board rules mirror the sketch: watchdog, caps, e-stop, joystick."""

import json

import pytest

from drive_service.chair.protocol import ChairHello, ChairStatus
from scripts.fake_chair import (
    BoardInputs,
    FakeBoard,
    apply_drive,
    board_output,
    hello_message,
    status_message,
)

NOW_MS = 1_000_000.0


def _drive(seq: int, v: float = 0.3, w: float = 0.2, ttl_ms: int = 200) -> str:
    return json.dumps(
        {"type": "drive", "seq": seq, "t": 0, "v_mps": v, "w_radps": w, "ttl_ms": ttl_ms}
    )


def _board(**caps) -> FakeBoard:
    return FakeBoard(max_speed_mps=caps.get("v", 0.5), max_turn_radps=caps.get("w", 0.5))


def test_no_command_yet_means_stopped_with_no_link():
    output = board_output(_board(), BoardInputs(), NOW_MS)
    assert (output.v_mps, output.w_radps, output.stopped_reason) == (0.0, 0.0, "no_link")


def test_a_fresh_command_is_applied():
    board = apply_drive(_board(), _drive(5), NOW_MS)
    output = board_output(board, BoardInputs(), NOW_MS + 100)
    assert (output.v_mps, output.w_radps, output.stopped_reason) == (0.3, 0.2, None)
    assert board.applied_seq == 5


def test_watchdog_stops_after_ttl():
    board = apply_drive(_board(), _drive(5, ttl_ms=200), NOW_MS)
    output = board_output(board, BoardInputs(), NOW_MS + 200)
    assert (output.v_mps, output.w_radps, output.stopped_reason) == (0.0, 0.0, "watchdog")


def test_older_or_repeated_seq_is_ignored():
    board = apply_drive(_board(), _drive(5, v=0.1), NOW_MS)
    board = apply_drive(board, _drive(4, v=0.4), NOW_MS + 10)
    board = apply_drive(board, _drive(5, v=0.4), NOW_MS + 10)
    assert board.applied_seq == 5
    assert board_output(board, BoardInputs(), NOW_MS + 20).v_mps == 0.1


def test_over_cap_is_clamped_and_reported_as_limit():
    board = apply_drive(_board(v=0.2, w=0.1), _drive(1, v=0.4, w=-0.3), NOW_MS)
    output = board_output(board, BoardInputs(), NOW_MS + 1)
    assert (output.v_mps, output.w_radps, output.stopped_reason) == (0.2, -0.1, "limit")


@pytest.mark.parametrize(
    ("inputs", "reason", "mode"),
    [
        (BoardInputs(estop=True), "estop", "auto"),
        (BoardInputs(joystick_active=True), "joystick", "manual"),
    ],
)
def test_estop_and_joystick_stop_the_output(inputs, reason, mode):
    board = apply_drive(_board(), _drive(1), NOW_MS)
    message = status_message(board, inputs, NOW_MS + 1, wall_ms=NOW_MS)
    status = ChairStatus.model_validate_json(message)
    assert status.output.v_mps == 0.0
    assert status.stopped_reason == reason
    assert status.mode == mode


@pytest.mark.parametrize("text", ["garbage", _drive(1, v=-0.5), _drive(1, ttl_ms=900)])
def test_invalid_drive_messages_are_ignored(text):
    board = apply_drive(_board(), text, NOW_MS)
    assert board == _board()


def test_status_and_hello_match_the_service_models():
    board = apply_drive(_board(), _drive(9), NOW_MS)
    status = ChairStatus.model_validate_json(status_message(board, BoardInputs(), NOW_MS, NOW_MS))
    assert status.applied_seq == 9
    assert status.battery_pct is None
    hello = ChairHello.model_validate_json(hello_message(board))
    assert hello.max_speed_mps == 0.5
