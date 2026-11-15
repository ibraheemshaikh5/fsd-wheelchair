"""A run always leaves the chair stopped, whatever ends it."""

import math

import pytest

from fsd_ml.moves import Runner
from fsd_ml.pilot import Call


class FakeChair:
    """Stands in for the console's Session: a chair that goes where its stick says."""

    def __init__(self) -> None:
        self.is_armed = True
        self.page = True
        self.gyro = True
        self.turns = True
        self.takeover_at: int | None = None
        self.obstacle: str | None = None
        self.yaw = 0.0
        self.x = 0.0
        self.sending = False
        self.sticks: list[tuple[float, float]] = []
        self.calls: list[str] = []
        self.settings = {
            "drive_stick_pct": 12.0,
            "ramp_s": 0.0,
            "turn_stick_pct": 3.0,
            "turn_lead_deg": 10.0,
            "turn_done_deg": 5.0,
            "turn_passes": 4.0,
            "settle_s": 0.3,
        }

    def armed(self):
        return self.is_armed

    def page_alive(self):
        return self.page

    def begin(self):
        self.sending = True
        return None

    def end(self):
        self.sending = False
        self.calls.append("end")

    def hold(self):
        self.calls.append("hold")

    def yaw_rad(self):
        return self.yaw

    def gyro_fresh(self):
        return self.gyro

    def setting(self, name):
        return self.settings[name]

    def speed_for(self, y_pct):
        return y_pct / 12 * 0.2

    def blocked(self):
        return self.obstacle

    def observe(self):
        return type("Frame", (), {"jpeg": b"jpeg"})(), "nothing sensed"

    def note(self, event, **fields):
        pass

    def record(self, mode, frame, result=None, model=None):
        pass

    def finished(self, record):
        pass

    def drive(self, x_pct, y_pct, reverse=False):
        assert self.sending
        assert y_pct >= 0 or reverse
        self.sticks.append((x_pct, y_pct))
        if self.takeover_at == len(self.sticks):
            return "the rider took over"
        if self.turns:
            self.yaw += math.radians(-x_pct * 0.4)  # stick right turns the chair right
        self.x += y_pct / 12 * 0.2 * 0.1
        return None


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def runner(chair):
    clock = Clock()
    return Runner(chair, clock=clock, sleep=clock.sleep), clock


def stopped(chair):
    return chair.calls[-1] == "end" and not chair.sending


def test_a_joystick_takeover_ends_the_run_at_once():
    chair = FakeChair()
    chair.takeover_at = 3
    done = runner(chair)[0].straight(2.0, background=False)
    assert done["outcome"] == "aborted" and "took over" in done["reason"]
    assert len(chair.sticks) == 3 and stopped(chair)


def test_disarming_stops_it_and_nothing_starts_unarmed():
    chair = FakeChair()
    drive = chair.drive

    def drive_then_disarm(x, y, reverse=False):
        chair.is_armed = len(chair.sticks) < 2
        return drive(x, y, reverse)

    chair.drive = drive_then_disarm
    run, _ = runner(chair)
    done = run.straight(2.0, background=False)
    assert done["outcome"] == "aborted" and done["reason"] == "disarmed"
    assert len(chair.sticks) == 3 and stopped(chair)
    with pytest.raises(RuntimeError, match="not armed"):
        run.straight(2.0, background=False)


def test_the_run_stops_when_the_page_stops_watching():
    chair = FakeChair()
    drive = chair.drive

    def drive_then_close_the_page(x, y, reverse=False):
        chair.page = len(chair.sticks) < 2
        return drive(x, y, reverse)

    chair.drive = drive_then_close_the_page
    done = runner(chair)[0].straight(2.0, background=False)
    assert done["outcome"] == "aborted" and "page" in done["reason"]
    assert len(chair.sticks) == 3 and stopped(chair)


def test_an_error_inside_a_run_still_stops_the_chair():
    chair = FakeChair()

    def broken(x, y, reverse=False):
        raise ValueError("it fell over")

    chair.drive = broken
    done = runner(chair)[0].straight(1.0, background=False)
    assert done["outcome"] == "failed" and "it fell over" in done["reason"]
    assert stopped(chair)


def test_a_turn_that_cannot_be_measured_or_does_not_happen_is_stopped():
    # A chair that will not turn is given up on, not spun for ever.
    chair = FakeChair()
    chair.turns = False
    done = runner(chair)[0].turn(90, background=False)
    assert done["outcome"] == "failed" and "not turning" in done["reason"] and stopped(chair)

    # No gyro: the turn never starts.
    chair = FakeChair()
    chair.gyro = False
    done = runner(chair)[0].turn(90, background=False)
    assert done["outcome"] == "failed" and "gyro" in done["reason"] and chair.sticks == []

    # The gyro goes quiet part way: the stick is let go at once.
    chair = FakeChair()
    drive = chair.drive

    def drive_then_lose_the_gyro(x, y, reverse=False):
        chair.gyro = len(chair.sticks) < 2
        return drive(x, y, reverse)

    chair.drive = drive_then_lose_the_gyro
    done = runner(chair)[0].turn(90, background=False)
    assert done["outcome"] == "failed" and "gyro" in done["reason"]
    assert len(chair.sticks) == 3 and stopped(chair)


class ScriptedPilot:
    """Stands in for the model: replies with the moves it was scripted to make."""

    def __init__(self, replies):
        self.replies = [
            [Call(f"c{i}{j}", name, dict(inputs)) for j, (name, inputs) in enumerate(reply)]
            for i, reply in enumerate(replies)
        ]
        self.told: list[list | None] = []
        self.last = {"stop_reason": "tool_use", "text": ""}

    def next(self, jpeg, report, results=None):
        self.told.append(results)
        return self.replies.pop(0) if self.replies else []


def test_what_the_pilot_asks_for_is_cut_to_what_is_allowed():
    chair = FakeChair()
    pilot = ScriptedPilot(
        [
            [("move_forward", {"distance_m": 25, "why": "far"})],
            [("move_forward", {"distance_m": -3, "why": "back, by the wrong tool"})],
            [("move_backward", {"distance_m": 9, "why": "far back"})],
            [("done", {"arrived": True, "why": "ok"})],
        ]
    )
    runner(chair)[0].pilot("go", pilot, background=False)
    forward = [y for _, y in chair.sticks if y > 0]
    back = [y for _, y in chair.sticks if y < 0]
    # A metre forward at most, 0.4 m back at most (0.2 m/s believed, a command every 0.1 s).
    assert 45 <= len(forward) <= 55 and 15 <= len(back) <= 25
    assert "the most allowed" in pilot.told[1][0][1]
    # A negative distance is refused, not driven the other way or as its absolute value.
    assert pilot.told[2][0][2] and "not a positive amount" in pilot.told[2][0][1]
    assert stopped(chair)


def test_an_obstacle_cuts_a_move_short_and_the_rest_of_the_reply_is_not_run():
    chair = FakeChair()
    chair.obstacle = "Waiting for obstacle"
    pilot = ScriptedPilot(
        [
            [
                ("move_forward", {"distance_m": 0.5, "why": "go"}),
                ("turn_right", {"degrees": 30, "why": "then"}),
            ],
            [("done", {"arrived": False, "why": "blocked"})],
        ]
    )
    done = runner(chair)[0].pilot("go", pilot, background=False)
    assert done["outcome"] == "failed"
    first, second = pilot.told[1]
    assert first[2] and "obstacle" in first[1]
    assert second[2] and "Not run" in second[1]
    assert chair.sticks == [] and stopped(chair)  # neither move happened
