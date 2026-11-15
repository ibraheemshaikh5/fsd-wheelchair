"""A run always leaves the chair stopped, whatever ends it."""

import io
import math

import pytest
from PIL import Image

from fsd_ml import bench, candidates
from fsd_ml.chair import ChairSetup
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
            "turn_min_pct": 1.0,
            "turn_max_pct": 3.0,
            "turn_slow_deg": 15.0,
            "turn_coast_s": 0.0,
            "turn_done_deg": 3.0,
            "settle_s": 0.3,
            "v_full_mps": 1.5,
            "w_full_radps": 1.5,
        }
        self.setup = ChairSetup()
        self.pictures: list[bytes] = []
        self.ended: list[str] = []
        # What the camera says of movement (None: no picture to compare), and how far the
        # chair veers by itself each time it is driven, in degrees to the left.
        self.change: float | None = None
        self.veer = 0.0

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

    def picture_change(self):
        return self.change

    def blocked(self, speed_mps=0.0, path_xy=None, obstacles=None):
        return self.obstacle

    def observe(self):
        return type("Frame", (), {"jpeg": picture()})(), "nothing sensed"

    def candidates(self):
        return candidates.fan((), self.setup, 0.25)

    def note(self, event, **fields):
        if event == "action_end":
            self.ended.append(fields.get("ended"))

    def record(self, mode, frame, result=None, model=None, picture=None):
        if picture:
            self.pictures.append(picture)

    def finished(self, record):
        pass

    def drive(self, x_pct, y_pct, reverse=False):
        assert self.sending
        assert y_pct >= 0 or reverse
        self.sticks.append((x_pct, y_pct))
        if self.takeover_at == len(self.sticks):
            return "the rider took over"
        if self.turns:
            self.yaw += math.radians(-x_pct * 0.4 + self.veer)  # stick right turns it right
        self.x += y_pct / 12 * 0.2 * 0.1
        return None


def picture() -> bytes:
    jpeg = io.BytesIO()
    Image.new("RGB", (480, 270)).save(jpeg, "JPEG")
    return jpeg.getvalue()


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
    assert done["outcome"] == "aborted" and done["reason"].endswith("then stopped: disarmed")
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
    assert done["outcome"] == "failed" and "isn't moving" in done["reason"] and stopped(chair)
    assert done["reason"].startswith("asked left 90°, gyro measured left 0°")

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
    # A metre forward at most (cut further by the 4 s move cap), 0.4 m back at most
    # (0.2 m/s believed, a command every 0.1 s).
    assert 35 <= len(forward) <= 41 and 15 <= len(back) <= 25
    # It is told what was allowed and how far the chair is believed to have got, not what
    # it asked for.
    assert pilot.told[1][0][1].startswith("Drove forward about 0.8")
    assert (
        "of 1.00 m (believed)" in pilot.told[1][0][1]
        and "1 m is what is allowed" in pilot.told[1][0][1]
    )
    # A negative distance is refused, not driven the other way or as its absolute value.
    assert pilot.told[2][0][2] and "not a positive amount" in pilot.told[2][0][1]
    assert stopped(chair)


def test_an_obstacle_cuts_a_move_short_and_the_rest_of_the_reply_is_not_run():
    chair = FakeChair()
    chair.obstacle = "obstacle 0.8 m ahead of the chair's front, 0.2 m to the right"
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
    assert first[2] and first[1].startswith("Drove forward about 0.00 m of 0.50 m (believed)")
    assert "then stopped: obstacle 0.8 m ahead" in first[1]
    assert second[2] and "Not run" in second[1]
    assert chair.sticks == [] and stopped(chair)  # neither move happened


class Picks:
    """Stands in for GeminiPilot: gives the picks it was made with, in order."""

    task = "go to the door"

    def __init__(self, *picks):
        self.picks, self.last, self.told = list(picks), {}, []

    def next(self, picture, options, outcome, heading_deg):
        self.told.append((outcome, heading_deg))
        self.last = {"choice": self.picks.pop(0)}
        return self.last["choice"] if self.last["choice"] in options else None


def test_gemini_follows_the_path_it_picks_and_is_told_how_it_went():
    chair = FakeChair()
    chair.settings["drive_stick_pct"] = 20.0  # fast enough to finish the path inside its cap
    pilot = Picks("1", "S")
    done = runner(chair)[0].gemini("go to the door", pilot, background=False)
    assert done["outcome"] == "arrived" and stopped(chair)
    # Path 1 curves left: the chair steered left along it, and the next look was told so.
    assert chair.yaw > math.radians(15) and all(y >= 0 for _, y in chair.sticks)
    assert "What happened: drove about" in pilot.told[1][0] and pilot.told[1][1] > 15
    assert len(chair.pictures) == 2  # every look's drawn picture goes to the record


def test_gemini_run_ends_on_a_pick_that_was_not_offered():
    chair = FakeChair()
    chair.candidates = dict  # everything blocked: only turns and ends on offer
    done = runner(chair)[0].gemini("go to the door", Picks("4"), background=False)
    assert done["outcome"] == "failed" and not chair.sticks and stopped(chair)


class Coasting(FakeChair):
    """A chair like the real one when turning: it answers late, its turn rate lags the stick
    (so it coasts on 12-17 degrees from full turn stick), and a small stick does not move it."""

    def __init__(self, dead_pct=5.0, lag_s=0.45, delay_s=0.2):
        super().__init__()
        self.settings.update(bench.DEFAULTS)
        self.dead_pct, self.lag_s, self.delay_s = dead_pct, lag_s, delay_s
        self.now, self.rate, self.stick, self.queue = 0.0, 0.0, 0.0, []

    def drive(self, x_pct, y_pct, reverse=False):
        self.queue.append((self.now + self.delay_s, x_pct))

    def hold(self):
        self.queue.append((self.now + self.delay_s, 0.0))

    def clock(self):
        return self.now

    def sleep(self, seconds):
        for _ in range(round(seconds / 0.01)):
            self.now += 0.01
            while self.queue and self.queue[0][0] <= self.now:
                self.stick = self.queue.pop(0)[1]
            push = abs(self.stick) - self.dead_pct  # 27 degrees a second 7% past it
            want = -math.copysign(27.0 * push / 7, self.stick) if push > 0 else 0
            self.rate += (want - self.rate) * 0.01 / self.lag_s
            self.yaw += math.radians(self.rate * 0.01)


# The last is a chair that 12% turn stick does not move at all: the stick is raised until it does.
@pytest.mark.parametrize(
    "plant", [{}, {"dead_pct": 9.0}, {"lag_s": 0.65, "delay_s": 0.35}, {"dead_pct": 16.0}]
)
def test_a_turn_ends_near_its_goal_without_hunting_on_a_chair_that_coasts(plant):
    for asked in (10, 25, -50, 90):
        chair = Coasting(**plant)
        chair.sending = True
        sent = []
        chair.drive = lambda x, y, reverse=False, sent=sent, chair=chair: (
            sent.append(x),
            Coasting.drive(chair, x, y, reverse),
        )[1]
        run = Runner(chair, clock=chair.clock, sleep=chair.sleep)
        run._state = {"running": True}
        outcome, said = run._turn(asked)
        # Within 3 degrees on the chair as measured; never left more than 5 past the goal.
        assert outcome == "finished", said
        assert abs(math.degrees(chair.yaw) - asked) <= (3.5 if not plant else 5.5), said
        # The stick changes side only to come back from a real overshoot: no hunting.
        assert sum(1 for a, b in zip(sent, sent[1:], strict=False) if a * b < 0) <= 2


def test_a_straight_holds_its_heading_and_gives_up_on_a_chair_that_is_not_moving():
    # A chair that veers right half a degree every tenth of a second is steered back.
    chair = FakeChair()
    chair.veer = -0.5
    done = runner(chair)[0].straight(0.6, background=False)
    assert done["outcome"] == "finished" and abs(math.degrees(chair.yaw)) < 3
    steer = [x for x, _ in chair.sticks]
    assert max(steer) <= 0 and -10 <= min(steer) < 0  # stick left, within its limit

    # The stick is held, the gyro is quiet and the picture is not changing: stuck.
    chair = FakeChair()
    chair.change = 0.3
    done = runner(chair)[0].straight(0.6, background=False)
    assert done["outcome"] == "failed" and "isn't moving" in done["reason"] and stopped(chair)
    assert 15 <= len(chair.sticks) <= 17 and done["reason"].startswith("drove forward about 0.0")


def test_a_route_whose_end_is_beside_the_chair_is_finished_not_waited_on_for_ever():
    from fsd_ml.route_pilot import Plan
    from fsd_ml.route_run import RouteRun

    class Draws:
        """A pilot that draws one short route, then says it has arrived."""

        task, last = "go", {}

        def __init__(self):
            self.asked = 0

        def next(self, picture, report):
            self.asked += 1
            if self.asked > 1:
                return Plan("arrived", note="there")
            # Two points on the floor about 1 m ahead, as fractions of the picture.
            pixels = chair.setup.to_pixels([[1.0, 0.0], [1.3, 0.0]], 1000, 1000)
            return Plan("route", points=tuple((x / 1000, y / 1000) for x, y in pixels))

    chair = FakeChair()
    chair.settings.update(bench.DEFAULTS)
    chair.settings["ramp_s"] = 0.0
    chair.speed_for = lambda y_pct: max(0.0, y_pct) / 100 * 7.5
    chair.ground = lambda frame: None
    chair.sighted = lambda: None
    chair.observe = lambda: (
        type("Frame", (), {"jpeg": picture(), "age_s": lambda self, now: 0.0})(),
        "",
    )
    # The chair really goes nowhere sideways, but is believed to: its gyro says it has
    # swung, so the end of the route comes level to one side, never under it.
    drive = chair.drive

    def veering(x, y, reverse=False):
        chair.yaw += math.radians(1.5)
        return drive(0.0, y, reverse)

    chair.drive = veering
    run, clock = runner(chair)
    run._state = {"running": True}
    chair.sending = True
    outcome, _ = RouteRun(run, Draws(), max_looks=5).run()
    # It gets to ask again (and is told it has arrived) instead of standing with the route.
    assert outcome == "arrived"
    ended = [why for why in chair.ended if why in ("finished", "stalled")]
    assert ended
