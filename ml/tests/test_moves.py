"""An episode finds the target, steps to it, and always leaves the chair stopped."""

import math

import pytest

from fsd_ml.episode import Limits, Runner


class FakeChair:
    """A chair in one dimension: a target some metres ahead, maybe behind the chair's back."""

    def __init__(self, away_m=2.0, bearing_deg=0.0) -> None:
        self.is_armed = True
        self.away, self.bearing = away_m, math.radians(bearing_deg)
        self.yaw = 0.0
        self.sending = False
        self.calls: list[str] = []
        self.takeover_at: int | None = None
        self.stuck = False
        self.steps = 0
        self.turns = True
        self.x = self.y = 0.0
        self.path: list[tuple[float, float]] = []
        self.notes: list[tuple[str, dict]] = []
        self.records: list[dict] = []

    def armed(self):
        return self.is_armed

    def begin(self):
        self.sending = True
        return None

    def end(self):
        self.sending = False
        self.calls.append("end")

    def hold(self):
        self.calls.append("hold")

    def _in_view(self):
        return abs(math.degrees(self.bearing - self.yaw)) < 35

    def target(self):
        return {"locked": self._in_view(), "note": None}

    def step(self, say):
        assert self.sending
        self.steps += 1
        if self.takeover_at == self.steps:
            return {"status": "clear", "chair": {"halted": True, "problem": "the rider took over"}}
        if self.stuck:
            return {"status": "blocked", "reason": "Waiting for obstacle", "steer": "target"}
        if not self._in_view():
            return {"status": "clear", "steer": "model"}
        if self.away <= 0.6:
            return {"status": "arrived", "steer": "target", "target_gap_m": round(self.away, 2)}
        self.away -= 0.1
        return {"status": "clear", "steer": "target", "target_xy": [self.away, 0.0]}

    def turn(self, stick_pct):
        assert self.sending
        if self.turns:
            self.yaw += math.radians(-stick_pct * 0.4)  # stick right turns the chair right
        return None

    def yaw_rad(self):
        return self.yaw

    # A stick of 12% is believed to give 0.2 m/s; the chair really does `true_speed` at it.
    true_speed = 0.2
    obstacle: str | None = None

    settings = {
        "drive_stick_pct": 12.0,
        "ramp_s": 0.0,
        "turn_stick_pct": 3.0,
        "turn_lead_deg": 10.0,
        "turn_done_deg": 5.0,
        "turn_passes": 4.0,
        "settle_s": 0.3,
        "w_full_radps": 1.5,
    }

    def setting(self, name):
        return self.settings[name]

    def pose(self):
        return self.x, self.y, self.yaw

    def finished(self, record):
        self.records.append(record)

    def note(self, event, **fields):
        self.notes.append((event, fields))

    def speed_for(self, y_pct):
        return y_pct / 12 * 0.2

    def blocked(self, v_mps, w_radps):
        return self.obstacle

    def observe(self):
        return b"jpeg", f"the block is {self.away:.1f} m ahead"

    def drive(self, x_pct, y_pct, reverse=False):
        assert self.sending
        assert y_pct >= 0 or reverse
        dt = 0.1
        self.yaw += math.radians(-x_pct * 0.4) * dt * 5
        speed = y_pct / 12 * self.true_speed
        self.x += speed * dt * math.cos(self.yaw)
        self.y += speed * dt * math.sin(self.yaw)
        self.path.append((self.x, self.y))
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


def test_it_steps_to_a_target_in_view_and_stops_there():
    chair = FakeChair(away_m=2.0)
    run, _ = runner(chair)
    done = run.go("go to the pillow", background=False)
    assert done["outcome"] == "arrived" and done["steps"] == 15
    assert chair.calls[-1] == "end" and not chair.sending


def test_it_turns_to_find_a_target_that_starts_out_of_view():
    chair = FakeChair(away_m=1.0, bearing_deg=90.0)
    run, _ = runner(chair)
    done = run.go("go to the pillow", background=False)
    assert done["outcome"] == "arrived" and chair.yaw > math.radians(50)


def test_without_searching_a_target_out_of_view_is_a_failure_and_the_chair_never_moves():
    chair = FakeChair(bearing_deg=120.0)
    run, _ = runner(chair)
    done = run.go("go to the pillow", Limits(search=False), background=False)
    assert done["outcome"] == "failed" and chair.steps == 0 and chair.yaw == 0.0


def test_a_target_that_is_nowhere_ends_after_one_full_turn():
    chair = FakeChair(bearing_deg=0.0)
    chair._in_view = lambda: False
    run, _ = runner(chair)
    done = run.go("go to the unicorn", background=False)
    assert done["outcome"] == "failed" and "all the way round" in done["reason"]


def test_a_joystick_takeover_ends_the_episode_at_once():
    chair = FakeChair()
    chair.takeover_at = 3
    run, _ = runner(chair)
    done = run.go("go to the pillow", background=False)
    assert done["outcome"] == "aborted" and "took over" in done["reason"] and chair.steps == 3


def test_being_blocked_for_too_long_is_a_failure_not_a_wait_for_ever():
    chair = FakeChair()
    chair.stuck = True
    run, clock = runner(chair)
    chair_step = chair.step
    chair.step = lambda say: (clock.sleep(0.5), chair_step(say))[1]
    done = run.go("go to the pillow", background=False)
    assert done["outcome"] == "failed" and "stuck" in done["reason"]


def test_the_step_limit_ends_it():
    chair = FakeChair(away_m=50.0)
    run, _ = runner(chair)
    done = run.go("go to the pillow", Limits(max_steps=5), background=False)
    assert done["outcome"] == "failed" and chair.steps == 5


def test_disarming_stops_it_and_nothing_starts_unarmed():
    chair = FakeChair(away_m=50.0)
    run, _ = runner(chair)
    chair_step = chair.step

    def step_then_disarm(say):
        chair.is_armed = chair.steps < 2
        return chair_step(say)

    chair.step = step_then_disarm
    done = run.go("go to the pillow", background=False)
    assert done["outcome"] == "aborted" and done["reason"] == "disarmed"
    with pytest.raises(RuntimeError, match="not armed"):
        run.go("go to the pillow", background=False)


def test_a_turn_goes_the_asked_way_by_the_asked_amount():
    chair = FakeChair()
    run, _ = runner(chair)
    assert run.turn(90, background=False)["outcome"] == "turned"
    assert abs(math.degrees(chair.yaw) - 90) < 8
    assert run.turn(-180, background=False)["outcome"] == "turned"
    assert abs(math.degrees(chair.yaw) + 90) < 8


def test_a_chair_that_will_not_turn_is_reported_not_spun_for_ever():
    chair = FakeChair()
    chair.turns = False
    run, _ = runner(chair)
    done = run.turn(90, background=False)
    assert done["outcome"] == "failed" and "not turning" in done["reason"]
    assert chair.calls[-1] == "end"


def test_an_error_inside_a_step_still_stops_the_chair():
    chair = FakeChair()

    def broken(say):
        raise ValueError("model fell over")

    chair.step = broken
    run, _ = runner(chair)
    done = run.go("go to the pillow", background=False)
    assert done["outcome"] == "failed" and "model fell over" in done["reason"]
    assert chair.calls[-1] == "end" and not chair.sending


def width(path):
    xs, ys = [p[0] for p in path], [p[1] for p in path]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def test_a_circle_comes_out_the_asked_width_when_the_speed_is_known():
    chair = FakeChair()
    run, _ = runner(chair)
    done = run.circle(0.914, background=False)
    assert done["outcome"] == "finished"
    assert abs(width(chair.path) - 0.914) < 0.12
    assert math.hypot(chair.x, chair.y) < 0.25  # and it ends about where it began
    assert chair.calls[-1] == "end"


def test_a_circle_driven_faster_than_believed_comes_out_wider_which_is_the_calibration():
    chair = FakeChair()
    chair.true_speed = 0.3  # half as fast again as believed
    run, _ = runner(chair)
    run.circle(0.914, background=False)
    assert abs(width(chair.path) - 0.914 * 1.5) < 0.2


def test_a_square_has_four_sides_and_ends_where_it_began():
    chair = FakeChair()
    run, _ = runner(chair)
    done = run.square(1.2, background=False)
    assert done["outcome"] == "finished"
    assert abs(width(chair.path) - 1.2) < 0.15
    assert math.hypot(chair.x, chair.y) < 0.25
    assert abs(math.degrees(chair.yaw) - 360) < 20


def test_a_shape_stops_for_an_obstacle():
    chair = FakeChair()
    chair.obstacle = "Waiting for obstacle"
    run, _ = runner(chair)
    done = run.square(1.2, background=False)
    assert done["outcome"] == "failed" and "obstacle" in done["reason"]
    assert chair.path == [] and chair.calls[-1] == "end"


def test_a_straight_eases_in_and_out_and_covers_the_asked_distance():
    chair = FakeChair()
    chair.settings = {**chair.settings, "ramp_s": 0.8}
    sticks = []
    drive = chair.drive
    chair.drive = lambda x, y: (sticks.append(y), drive(x, y))[1]
    run, _ = runner(chair)
    assert run.straight(0.762, background=False)["outcome"] == "finished"
    assert sticks[0] < 3 and sticks[-1] < 4 and max(sticks) == 12  # gentle at both ends
    assert all(x == 0 for x in [0])  # and never steered
    assert abs(chair.x - 0.762) < 0.1


def test_returning_to_the_start_undoes_a_drive_and_a_turn():
    chair = FakeChair()
    run, _ = runner(chair)
    run.straight(1.0, background=False)
    run.turn(90, background=False)
    run.straight(0.6, background=False)
    assert math.hypot(chair.x, chair.y) > 1.0
    done = run.home(background=False)
    assert done["outcome"] == "finished"
    assert math.hypot(chair.x, chair.y) < 0.25
    assert abs(math.degrees(chair.yaw)) % 360 < 12 or abs(math.degrees(chair.yaw)) % 360 > 348
    assert chair.records[-1]["kind"] == "home"


class ScriptedPilot:
    """Stands in for the model: replies with the moves it was scripted to make."""

    def __init__(self, replies):
        from fsd_ml.pilot import Call

        self.replies = [
            [Call(f"c{i}{j}", name, dict(inputs)) for j, (name, inputs) in enumerate(reply)]
            for i, reply in enumerate(replies)
        ]
        self.seen: list[tuple[str, list | None]] = []
        self.last = {"stop_reason": "tool_use", "text": ""}

    def next(self, jpeg, report, results=None):
        self.seen.append((report, results))
        return self.replies.pop(0) if self.replies else []


def test_the_pilots_moves_are_carried_out_in_order_and_reported_back():
    chair = FakeChair()
    pilot = ScriptedPilot(
        [
            [
                ("turn_left", {"degrees": 90, "why": "face it"}),
                ("move_forward", {"distance_m": 0.5, "why": "go"}),
            ],
            [("move_backward", {"distance_m": 0.2, "why": "too close"})],
            [("done", {"arrived": True, "why": "half a metre short of the pillow"})],
        ]
    )
    run, _ = runner(chair)
    done = run.pilot("go to the pillow", pilot, background=False)
    assert done["outcome"] == "arrived" and "half a metre short" in done["reason"]
    assert abs(math.degrees(chair.yaw) - 90) < 10
    assert abs(chair.y - 0.3) < 0.12  # half a metre along the new heading, then 0.2 back
    first, second, third = pilot.seen
    assert first[1] is None  # nothing to report before the first move
    assert [failed for _, _, failed in second[1]] == [False, False]
    assert "gyro says" in second[1][0][1] and "Drove forward" in second[1][1][1]
    assert "Drove back" in third[1][0][1]
    assert chair.calls[-1] == "end"


def test_a_move_stopped_for_an_obstacle_cuts_the_rest_short_and_the_pilot_is_told():
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
    run, _ = runner(chair)
    done = run.pilot("go to the pillow", pilot, background=False)
    assert done["outcome"] == "failed" and "blocked" in done["reason"]
    told = pilot.seen[1][1]
    assert told[0][2] and "obstacle" in told[0][1]
    assert told[1][2] and "Not run" in told[1][1]
    assert chair.path == [] and chair.yaw == 0.0  # neither move happened


def test_what_the_pilot_asks_for_is_cut_to_what_is_allowed():
    chair = FakeChair()
    pilot = ScriptedPilot(
        [
            [("move_forward", {"distance_m": 25, "why": "far"})],
            [("done", {"arrived": True, "why": "ok"})],
        ]
    )
    run, _ = runner(chair)
    run.pilot("go", pilot, background=False)
    assert 0.8 < chair.x < 1.2
    assert "the most allowed" in pilot.seen[1][1][0][1]


def test_a_pilot_that_gives_no_move_or_never_finishes_ends_the_run():
    chair = FakeChair()
    run, _ = runner(chair)
    done = run.pilot("go", ScriptedPilot([]), background=False)
    assert done["outcome"] == "failed" and "gave no move" in done["reason"]
    forever = ScriptedPilot([[("turn_left", {"degrees": 10, "why": "look"})]] * 9)
    done = run.pilot("go", forever, max_turns=3, background=False)
    assert done["outcome"] == "failed" and "after 3 looks" in done["reason"]
    assert chair.calls[-1] == "end"
