"""ChairSender turns model decisions into Wheelchair.set(x, y) and holds them briefly."""

import time

import pytest

from fsd_ml.chair_out import MAX_PCT, ChairSender, to_stick


class FakeChair:
    """Stands in for hardware/wheelchair.py's Wheelchair: no network."""

    def __init__(self) -> None:
        self.fsd = False
        self.sets: list[tuple[int, int]] = []
        self.board = {"takeover": False, "dropout_latched": False}
        self.closed = False

    def set(self, x: int, y: int) -> None:
        self.sets.append((x, y))

    def stop(self) -> None:
        self.set(0, 0)

    def stats(self) -> dict:
        return dict(self.board)

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_mapping_turns_speed_into_forward_and_left_turns_into_negative_x():
    assert to_stick(0.0, 0.0) == (0, 0)
    assert to_stick(0.3, 0.0) == (0, 20)  # 0.3 / 1.5 m/s full scale
    assert to_stick(0.0, 0.15) == (-10, 0)  # left is stick left
    assert to_stick(0.0, -0.15) == (10, 0)


def test_mapping_never_reverses_and_caps_the_stick():
    assert to_stick(-1.0, 0.0) == (0, 0)
    assert to_stick(10.0, -10.0) == (MAX_PCT, MAX_PCT)
    assert to_stick(0.0, 10.0) == (-MAX_PCT, 0)


def test_sender_switches_fsd_on_sends_and_stops():
    chair = FakeChair()
    sender = ChairSender(chair)
    assert chair.fsd is True
    result = sender.send(0.3, 0.15)
    assert result.sent and (result.x, result.y) == (-10, 20)
    assert chair.sets[0] == (-10, 20)
    assert sender.stop().sent and chair.sets[-1] == (0, 0)
    sender.close()
    assert chair.closed


def test_a_decision_is_repeated_until_its_hold_runs_out():
    chair, clock = FakeChair(), Clock()
    sender = ChairSender(chair, hold_ms=600, clock=clock)
    sender.send(0.3, 0.0)
    time.sleep(0.35)
    assert chair.sets.count((0, 20)) >= 3  # the hold thread keeps calling set at 10 Hz
    clock.now += 0.7
    time.sleep(0.15)
    count = len(chair.sets)
    time.sleep(0.3)
    assert len(chair.sets) == count  # expired: set stops, and the Wheelchair zeroes itself
    sender.close()


def test_stop_ends_the_hold_at_once():
    chair = FakeChair()
    sender = ChairSender(chair, clock=Clock())
    sender.send(0.3, 0.0)
    sender.stop()
    count = len(chair.sets)
    time.sleep(0.25)
    assert len(chair.sets) == count and chair.sets[-1] == (0, 0)
    sender.close()


@pytest.mark.parametrize("flag", ["takeover", "dropout_latched"])
def test_takeover_or_dropout_halts_sending(flag):
    chair = FakeChair()
    sender = ChairSender(chair, clock=Clock())
    sender.send(0.3, 0.0)
    chair.board[flag] = True
    time.sleep(0.15)
    count = len(chair.sets)
    time.sleep(0.25)
    assert len(chair.sets) == count  # the hold thread gave up
    result = sender.send(0.3, 0.0)
    assert result.halted and not result.sent and result.problem
    assert len(chair.sets) == count
    sender.close()
