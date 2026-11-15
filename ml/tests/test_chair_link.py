"""ChairSender: what reaches Wheelchair.set(x, y), and when it stops."""

import time

import pytest

from fsd_ml.chair_link import MAX_PCT, MIN_BRAKE_FROM_PCT, ChairSender


class FakeChair:
    """Stands in for hardware/wheelchair.py's Wheelchair: no network."""

    def __init__(self) -> None:
        self.fsd = False
        self.sets: list[tuple[int, int]] = []
        self.board = {"takeover": False, "dropout_latched": False}

    def set(self, x: int, y: int) -> None:
        self.sets.append((x, y))

    def stop(self) -> None:
        self.set(0, 0)

    def stats(self) -> dict:
        return dict(self.board)

    def close(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_a_command_is_repeated_until_its_hold_runs_out():
    chair, clock = FakeChair(), Clock()
    sender = ChairSender(chair, hold_ms=600, clock=clock)
    sender.send_stick(0, 20)
    time.sleep(0.35)
    assert chair.sets.count((0, 20)) >= 3  # the hold thread keeps calling set at 10 Hz
    clock.now += 0.7
    time.sleep(0.15)
    count = len(chair.sets)
    time.sleep(0.3)
    assert len(chair.sets) == count  # expired: set stops, and the Wheelchair zeroes itself
    sender.close()


@pytest.mark.parametrize("flag", ["takeover", "dropout_latched"])
def test_takeover_or_dropout_halts_sending(flag):
    chair = FakeChair()
    sender = ChairSender(chair, clock=Clock())
    sender.send_stick(0, 20)
    chair.board[flag] = True
    time.sleep(0.15)
    count = len(chair.sets)
    time.sleep(0.25)
    assert len(chair.sets) == count  # the hold thread gave up
    result = sender.send_stick(0, 20)
    assert result.halted and not result.sent and result.problem
    assert len(chair.sets) == count
    sender.close()


def test_the_stick_is_capped_and_never_reverses_unless_asked():
    chair = FakeChair()
    sender = ChairSender(chair, clock=Clock())
    result = sender.send_stick(-100, 12.4)
    assert (result.x, result.y) == (-MAX_PCT, 12)
    assert sender.send_stick(5, -20).y == 0
    assert sender.send_stick(0, -20, reverse=True).y == -20
    assert chair.sets == [(-MAX_PCT, 12), (5, 0), (0, -20)]
    sender.close()


def test_braking_pulls_back_only_when_the_chair_was_really_moving():
    chair, clock = FakeChair(), Clock()
    sender = ChairSender(chair, clock=clock, sleep=lambda _: None)
    # Standing still: a brake is a plain stop, never a reverse.
    assert sender.brake(10, 200).y == 0
    # Creeping, as at the end of an eased straight: still a plain stop.
    sender.send_stick(0, MIN_BRAKE_FROM_PCT - 1)
    assert sender.brake(10, 200).y == 0
    # Backing or turning on the spot: nothing to brake against.
    sender.send_stick(20, -20, reverse=True)
    assert sender.brake(10, 200).y == 0
    sender.send_stick(0, 20)
    chair.sets.clear()
    assert sender.brake(10, 200).y == -10
    assert chair.sets == [(0, -10)] * 4 + [(0, 0)]
    # Already braked: asking again does not pull back a second time.
    chair.sets.clear()
    sender.brake(90, 5000)
    assert chair.sets == [(0, 0)]
    # After a takeover the stick is the rider's: no pull.
    sender.send_stick(0, 20)
    chair.board["takeover"] = True
    chair.sets.clear()
    sender.brake(10, 200)
    assert chair.sets == [(0, 0)]
    sender.close()
