"""The commander: stop unless a bench test is running; caps always apply."""

import pytest

from drive_service.chair.commander import (
    BenchCommand,
    Caps,
    bench_active,
    command_lag_ms,
    effective_caps,
    next_command,
)
from drive_service.chair.protocol import ChairHello
from test_chair_support import chair_settings, hello_dict

CAPS = Caps(max_speed_mps=0.5, max_turn_radps=0.5)
WALL_MS = 1_794_650_000_123.7


def _next(bench=None, last_seq=None, now=10.0, caps=CAPS):
    return next_command(
        now_mono=now, wall_ms=WALL_MS, bench=bench, last_seq=last_seq, caps=caps, ttl_ms=200
    )


def test_stops_by_default():
    command = _next()
    assert (command.v_mps, command.w_radps) == (0.0, 0.0)
    assert command.seq == 0
    assert command.t == 1_794_650_000_123
    assert command.ttl_ms == 200


def test_seq_counts_up_by_one():
    assert _next(last_seq=41).seq == 42


def test_runs_an_active_bench_command():
    bench = BenchCommand(v_mps=0.2, w_radps=-0.1, expires_mono=11.0)
    command = _next(bench=bench, now=10.0)
    assert (command.v_mps, command.w_radps) == (0.2, -0.1)


def test_stops_once_the_bench_command_expires():
    bench = BenchCommand(v_mps=0.2, w_radps=0.1, expires_mono=10.0)
    command = _next(bench=bench, now=10.0)
    assert (command.v_mps, command.w_radps) == (0.0, 0.0)


@pytest.mark.parametrize(
    ("v", "w", "expected"),
    [
        (2.0, 0.0, (0.5, 0.0)),
        (0.1, 3.0, (0.1, 0.5)),
        (0.1, -3.0, (0.1, -0.5)),
        (-0.3, 0.0, (0.0, 0.0)),
    ],
)
def test_clamps_to_caps_and_never_reverses(v, w, expected):
    bench = BenchCommand(v_mps=v, w_radps=w, expires_mono=99.0)
    command = _next(bench=bench)
    assert (command.v_mps, command.w_radps) == expected


def test_bench_active_only_before_expiry():
    bench = BenchCommand(v_mps=0.1, w_radps=0.0, expires_mono=5.0)
    assert bench_active(bench, 4.999)
    assert not bench_active(bench, 5.0)
    assert not bench_active(None, 0.0)


def test_effective_caps_without_hello_are_the_service_caps():
    caps = effective_caps(chair_settings(), None)
    assert caps == Caps(max_speed_mps=0.5, max_turn_radps=0.5)


def test_effective_caps_take_the_lower_of_service_and_board():
    hello = ChairHello(**hello_dict(max_speed_mps=0.3, max_turn_radps=0.8))
    caps = effective_caps(chair_settings(), hello)
    assert caps == Caps(max_speed_mps=0.3, max_turn_radps=0.5)


@pytest.mark.parametrize(
    ("last_seq", "applied_seq", "lag_ms"),
    [
        (20, 10, 500.0),  # ten commands behind at 20 Hz
        (20, 20, 0.0),
        (20, None, 0.0),  # board stopped: nothing old is driving
        (None, 5, 0.0),  # nothing sent yet
        (3, 900, 0.0),  # a seq from before a service restart
    ],
)
def test_command_lag(last_seq, applied_seq, lag_ms):
    assert command_lag_ms(last_seq, applied_seq, rate_hz=20.0) == lag_ms
