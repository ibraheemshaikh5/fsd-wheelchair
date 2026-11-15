"""Carry out a route pilot's run: drive what it draws, without stopping to think.

    RouteRun(runner, pilot, max_looks).run()   # called by moves.Runner.route

The pilot (fsd_ml/route_pilot.py) is slow, a few seconds a look, and the chair should not
stand waiting for it. So the two go on side by side:

- The chair follows the route it has. Where it is, is kept as a believed pose in the run's
  own frame: heading from the gyro, distance from the calibrated speed of the stick sent.
- Once it has driven REPLAN_M of a route, the next look is asked for: the picture as it is
  then, with the pose it was taken at. When the answer comes, some seconds and a metre or
  two later, the new route is placed by that pose and taken up from wherever on it the
  chair has got to. If the chair gets to the end of its route first, it stands and waits.
- Standing still (at the start, after a turn, after being stopped), it waits for the
  picture to settle, asks, and waits for the answer.

What the model is not asked to keep in its head is kept here and given back to it: what is
left of its route and where it marked the goal (drawn on the next picture), what each move
actually did and why it ended, and how far the chair has turned and moved.

The stop is the runner's own, the same rule the route was cut by (Session.blocked on what
is left of the route), with the obstacles seen earlier in the run remembered after they
leave the picture (route_pilot.Remembered): the chair's body is beside and behind the
camera, so what it is passing is out of view while it can still be hit.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import TYPE_CHECKING, Any

import numpy as np

from fsd_ml import route_pilot
from fsd_ml.candidates import PLAN_SLACK_M
from fsd_ml.chair_link import to_stick
from fsd_ml.moves import STUCK, STUCK_S, _degrees
from fsd_ml.pipeline.control import follow
from fsd_ml.pipeline.footprint import SAMPLE_SPACING_M, stop_distance_m, turn_room_deg
from fsd_ml.route_pilot import Plan, Pose, Remembered, RoutePilot, into, out_of

if TYPE_CHECKING:
    from fsd_ml.moves import Runner

TICK_S = 0.1
# The next look is asked for once this much of the route in hand has been driven.
REPLAN_M = 0.5
# Standing still this long before a look, so the picture is sharp and from after the move.
SETTLE_S = 0.6
# A route whose start is further than this off the chair's heading is turned to face first.
PIVOT_DEG = 20.0
# A turn there is less room for than this is not made at all.
MIN_TURN_DEG = 10.0
# A new route is taken up on the move only if the chair is this near it and facing this
# nearly along it; otherwise the chair stops and starts it from standing.
JOIN_M = 0.4
JOIN_DEG = 35.0
# The run ends after this many answers in a row that could not be used, or with no camera
# picture for this long.
MAX_BAD_ANSWERS = 3
NO_PICTURE_S = 5.0


class RouteRun:
    def __init__(self, runner: Runner, pilot: RoutePilot, max_looks: int) -> None:
        self._runner, self._pilot, self._max_looks = runner, pilot, max_looks
        self._chair = runner._chair
        self._clock = runner._clock
        self._setup = self._chair.setup
        # The believed pose in the run's frame, the speed last commanded, and the poses of
        # the last few seconds (for placing a picture taken a moment ago). What is moved on
        # is the point the chair turns about (the middle of its drive wheels): it goes where
        # the chair points and stays put in a turn. The chair's own middle is ahead of it.
        self._pivot = self._setup.chair.pivot_behind_m
        self._axle = [-self._pivot, 0.0]
        self._xy = [0.0, 0.0]
        self._driven = 0.0
        self._v = 0.0
        self._at_s = self._clock()
        self._poses: deque[tuple[float, Pose]] = deque(maxlen=200)
        # The route in hand, in the run's frame; how far along it the chair is; and since
        # when it has been driving (None while standing).
        self._path: np.ndarray | None = None
        self._along = 0
        self._started_xy = (0.0, 0.0)
        self._moving_since: float | None = None
        self._stood_since = self._clock()
        self._quiet: tuple[float, float] | None = None
        self._was_yaw = self._chair.yaw_rad()
        # The look being asked, if any, and where the chair was when the last one was taken.
        self._asking: threading.Thread | None = None
        self._answer: dict[str, Any] | None = None
        self._asked_xy = (0.0, 0.0)
        self._looks = 0
        self._bad = 0
        self._no_picture_since: float | None = None
        # What the model is given back: its notes, what has happened since its last answer,
        # and where it last marked the goal (run's frame).
        self._notes: list[str] = []
        self._happened: list[str] = []
        self._goal: tuple[float, float] | None = None
        self._remembered = Remembered(self._setup)
        self._seen_s = 0.0

    def _pose(self) -> Pose:
        return (self._xy[0], self._xy[1], self._chair.yaw_rad())

    def _pose_at(self, when_s: float) -> Pose:
        """The pose nearest a moment in the last few seconds."""
        if not self._poses:
            return self._pose()
        return min(self._poses, key=lambda entry: abs(entry[0] - when_s))[1]

    def run(self) -> tuple[str, str]:
        while True:
            now = self._clock()
            if why := self._runner._stopped():
                self._drop("stopped", why)
                return "aborted", why
            self._move_on(now)
            self._remember()
            if self._answer is not None:
                done = self._take(self._answer)
                self._answer, self._asking = None, None
                if done:
                    return done
                continue
            if done := self._ask(now):
                return done
            if self._path is not None and (done := self._drive(now)):
                return done
            self._runner._sleep(TICK_S)

    def _move_on(self, now: float) -> None:
        """Move the believed pose on by what was last commanded."""
        yaw = self._chair.yaw_rad()
        step = self._v * (now - self._at_s)
        self._at_s = now
        self._driven += abs(step)
        self._axle[0] += step * math.cos(yaw)
        self._axle[1] += step * math.sin(yaw)
        self._xy[0] = self._axle[0] + self._pivot * math.cos(yaw)
        self._xy[1] = self._axle[1] + self._pivot * math.sin(yaw)
        self._poses.append((now, self._pose()))

    def _remember(self) -> None:
        """Take in the obstacle sense's newest picture, placed where the chair was then."""
        sighted = self._chair.sighted()
        if sighted is not None and sighted[1] != self._seen_s:
            self._seen_s = sighted[1]
            self._remembered.see(sighted[0], self._pose_at(sighted[1]), sighted[1], self._driven)

    def _rest(self, pose: Pose) -> np.ndarray:
        """What is left of the route in hand, in the chair's frame at ``pose``."""
        if self._path is None:
            return np.empty((0, 2))
        return into(pose, self._path[self._along :])

    def _ask(self, now: float) -> tuple[str, str] | None:
        """Start a look if it is time for one. Ends the run when there can be no more."""
        if self._asking is not None:
            return None
        if self._path is None:
            if now - self._stood_since < SETTLE_S:
                return None
        elif math.dist(self._xy, self._asked_xy) < REPLAN_M:
            return None
        if self._looks >= self._max_looks:
            if self._path is None:
                return "failed", f"not done after {self._max_looks} looks"
            return None
        frame, _ = self._chair.observe()
        if frame is None:
            self._no_picture_since = self._no_picture_since or now
            if now - self._no_picture_since > NO_PICTURE_S:
                self._drop("stale_camera", "no fresh camera picture")
                return "failed", (
                    "no camera picture newer than 0.5 s arrived within 5 s:"
                    " the phone's stream has stopped"
                )
            return None
        self._no_picture_since = None
        taken_s = now - frame.age_s(now)
        pose = self._pose_at(taken_s)
        rest = self._rest(pose)
        if self._path is None:
            moving = "The chair is standing still, waiting for your answer."
        else:
            left_m = len(rest) * SAMPLE_SPACING_M
            moving = (
                f"The chair is driving your last route ({left_m:.1f} m of it is left, the dashed"
                " cyan line) and will keep to it until your answer comes. Your new route"
                " replaces it."
            )
        goal = None if self._goal is None else tuple(into(pose, [self._goal])[0])
        self._looks += 1
        happened, self._happened = self._happened, []
        self._asked_xy = (self._xy[0], self._xy[1])
        self._runner._phase(f"look {self._looks}: the pilot is thinking", steps=self._looks)
        obstacles = self._remembered.around(pose)
        look = {"look": self._looks, "frame": frame, "pose": pose}
        notes, half_view = list(self._notes), self._setup.camera.hfov_deg / 2

        def ask() -> None:
            try:
                ground = self._chair.ground(frame)
                behind = goal is not None and route_pilot.hidden(goal, ground, self._setup)
                said = route_pilot.report(
                    look["look"], moving, happened, pose, goal, notes, half_view, behind
                )
                shown = route_pilot.picture(
                    frame.jpeg,
                    self._setup,
                    obstacles,
                    self._chair.setting("path_clearance_m"),
                    rest if len(rest) > 1 else None,
                    None if behind else goal,
                )
                plan = self._pilot.next(shown, said)
                self._answer = {**look, "shown": shown, "plan": plan, "ground": ground}
            except Exception as error:  # noqa: BLE001  a failed look must not end the driving
                self._answer = {**look, "error": repr(error)}

        self._asking = threading.Thread(target=ask, daemon=True)
        self._asking.start()
        return None

    def _take(self, answer: dict[str, Any]) -> tuple[str, str] | None:
        """Act on the pilot's answer. Returns how the run ended, if this ends it."""
        plan: Plan | None = answer.get("plan")
        pose: Pose = answer["pose"]
        record: dict[str, Any] = {
            "look": answer["look"],
            "say": self._pilot.task,
            "pose_when_taken": [round(pose[0], 3), round(pose[1], 3), round(pose[2], 4)],
            **self._pilot.last,
        }
        if plan is None:
            self._bad += 1
            why = answer.get("error") or "the answer was not one of the actions"
            self._chair.record("route", answer["frame"], model={**record, "error": why})
            self._happened.append(f"Your last answer could not be used ({why}).")
            if self._bad >= MAX_BAD_ANSWERS:
                self._drop("stopped", "the pilot gave no usable answer")
                return "failed", f"{self._bad} answers in a row could not be used: {why}"
            return None
        self._bad = 0
        self._notes.append(plan.note)
        ground = answer.get("ground")
        if plan.goal is not None:
            ahead, left, _ = route_pilot.place(plan.goal, ground, self._setup)
            if math.isfinite(ahead) and 0 < math.hypot(ahead, left) < 8:
                self._goal = tuple(out_of(pose, [[ahead, left]])[0])
        route = None
        if plan.action == "route":
            speed = self._chair.speed_for(self._chair.setting("drive_stick_pct"))
            route = route_pilot.interpret(
                plan,
                ground,
                self._remembered.around(pose),
                self._setup,
                self._chair.setting("path_clearance_m"),
                stop_distance_m(speed) + PLAN_SLACK_M,
            )
            record["route"] = {
                "points": [[round(a, 2), round(b, 2), floor] for a, b, floor in route.points],
                "path": np.round(route.path, 2).tolist(),
                "said": route.said,
            }
        self._chair.record("route", answer["frame"], model=record, picture=answer["shown"])
        self._runner._phase(f"look {answer['look']}: {plan.action} · {plan.note}")
        if plan.action in ("arrived", "give_up"):
            self._drop("replaced", f"the pilot answered {plan.action}")
            outcome = "arrived" if plan.action == "arrived" else "failed"
            return outcome, f"the pilot said: {plan.scene} {plan.note}"
        if route is not None:
            return self._adopt(route, pose)
        self._drop("replaced", f"the pilot answered {plan.action}")
        if plan.action == "back_up":
            outcome, text = self._runner._back(route_pilot.BACK_UP_M)
            backed = float(self._runner.achieved.get("believed_m", 0.0))
            yaw = self._chair.yaw_rad()
            self._axle[0] -= backed * math.cos(yaw)
            self._axle[1] -= backed * math.sin(yaw)
        else:
            outcome, text = self._turn(route_pilot.TURNS[plan.action])
        self._move_on(self._clock())
        self._stood_since = self._clock()
        self._happened.append(f"You answered {plan.action}: {text}.")
        return ("aborted", text) if outcome == "aborted" else None

    def _adopt(self, route: route_pilot.Route, pose: Pose) -> tuple[str, str] | None:
        """Take up a new route, placed where the chair was when its picture was taken."""
        if not len(route.path):
            keeps = " The chair keeps to your earlier route." if self._path is not None else ""
            self._happened.append(f"You drew a route. {route.said}{keeps}")
            return None
        path = out_of(pose, route.path)
        here = self._pose()
        gaps = np.linalg.norm(path - self._xy, axis=1)
        nearest = int(np.argmin(gaps))
        ahead = into(here, path[nearest : nearest + 6])
        if len(ahead) < 2:
            self._happened.append(
                "You drew a route, but the chair had already driven past its end when your"
                " answer came."
            )
            return None
        # Which way the route runs from where the chair joins it, against the chair's heading.
        to, start = ahead[min(len(ahead) - 1, 4)], (0.0, 0.0) if nearest == 0 else ahead[0]
        bearing = math.degrees(math.atan2(to[1] - start[1], to[0] - start[0]))
        said = f"You drew a route. {route.said}".strip()
        joins = self._path is not None and gaps[nearest] <= JOIN_M and abs(bearing) <= JOIN_DEG
        if joins:
            self._end_route("replaced", "finished")  # and drive on, onto the new one
        else:
            self._drop("replaced", "your new route starts away from where the chair was heading")
            if abs(bearing) > PIVOT_DEG:
                outcome, text = self._turn(bearing)
                self._move_on(self._clock())
                self._stood_since = self._clock()
                said += f" To face its start: {text}."
                if outcome == "aborted":
                    return "aborted", text
                if outcome == "failed":
                    self._happened.append(f"{said} The route was not driven.")
                    return None
        self._happened.append(said)
        self._path, self._along = path, nearest
        self._runner._begin(
            "route",
            length_m=round((len(path) - nearest) * SAMPLE_SPACING_M, 2),
            path=np.round(path, 2).tolist(),
        )
        self._started_xy = (self._xy[0], self._xy[1])
        return None

    def _turn(self, degrees_left: float) -> tuple[str, str]:
        """Turn on the spot, as far as the chair's body has room to: a turn swings its front
        and rear through floor the camera is not looking at, so what was seen there earlier
        in the run decides. What it could not turn is said."""
        body = self._setup.chair
        room = turn_room_deg(
            degrees_left, self._remembered.around(self._pose(), self._driven), body
        )
        if abs(room) < MIN_TURN_DEG:
            self._chair.note("refused", call="turn", asked_deg=degrees_left, room_deg=room)
            return "failed", (
                f"asked {_degrees(degrees_left)}, did not turn: the chair's body would hit"
                " something beside it that it saw earlier"
            )
        outcome, text = self._runner._turn(room)
        if room != degrees_left:
            text += (
                f" (you asked for {_degrees(degrees_left)}; further round, the chair's body"
                " would hit something beside it that it saw earlier)"
            )
        return outcome, text

    def _end_route(self, reason: str, outcome: str, why: str = "") -> str:
        """Close the record of the route in hand; says how it went, for the model."""
        driven = math.dist(self._xy, self._started_xy)
        left_m = (
            max(0, len(self._path) - self._along) * SAMPLE_SPACING_M
            if self._path is not None
            else 0.0
        )
        text = f"drove about {driven:.1f} m along your route ({left_m:.1f} m of it was left)"
        text += f", then stopped: {why}" if why else ""
        self._runner._end(
            reason,
            outcome,
            text,
            believed_m=round(driven, 2),
            left_m=round(left_m, 2),
            believed_pose=[round(self._xy[0], 2), round(self._xy[1], 2)],
        )
        self._path = None
        return text

    def _drop(self, reason: str, why: str) -> None:
        """Stop driving the route in hand, if there is one, and stand."""
        if self._path is not None:
            self._chair.hold()
            outcome = {"stopped": "aborted", "replaced": "finished"}.get(reason, "failed")
            text = self._end_route(reason, outcome, why)
            self._happened.append(f"The chair {text}.")
        self._v, self._moving_since = 0.0, None
        self._stood_since = self._clock()

    def _drive(self, now: float) -> tuple[str, str] | None:
        """One tick of following the route in hand."""
        chair = self._chair
        here = self._pose()
        # How far along the route the chair has got: the nearest point not behind where it was.
        window = self._path[self._along : self._along + 15]
        self._along += int(np.argmin(np.linalg.norm(window - self._xy, axis=1)))
        rest = into(here, self._path[self._along + 1 :])
        if len(rest) < 1:
            chair.hold()
            self._happened.append(f"The chair {self._end_route('finished', 'finished')}.")
            self._v, self._moving_since, self._stood_since = 0.0, None, now
            return None
        ahead = into(here, self._path[self._along :])
        if why := chair.blocked(self._v, ahead, self._remembered.around(here, self._driven)):
            self._drop("obstacle" if why.startswith("obstacle") else "no_floor", why)
            return None
        if not chair.gyro_fresh():
            self._drop("stale_gyro", "the gyro stopped reporting")
            return None
        stick = chair.setting("drive_stick_pct")
        speed = chair.speed_for(stick)
        turned = here[2] - self._was_yaw
        self._was_yaw = here[2]
        self._quiet = self._runner._quiet(self._quiet, 0.0, self._v >= speed / 2, turned, TICK_S)
        if self._quiet and now - self._quiet[0] >= STUCK_S:
            self._drop("stuck", STUCK)
            return None
        if self._moving_since is None:
            self._moving_since = now
        ramp = max(0.0, chair.setting("ramp_s"))
        left_m = len(rest) * SAMPLE_SPACING_M
        eased = 1.0
        if ramp > 0:
            eased = min(1.0, (now - self._moving_since) / ramp, left_m / (speed * ramp))
        v, w = follow(rest, max(eased, 1.0 / stick), speed_limit_mps=speed)
        if left_m < 0.3:
            w = 0.0  # too near the end to aim at it: the aim swings wide as it closes
        turn, forward = to_stick(v, w, chair.setting("v_full_mps"), chair.setting("w_full_radps"))
        # Never a harder turn than a turn on the spot is given: a curve too tight for that at
        # this speed is driven more slowly, not wider.
        slower = min(1.0, chair.setting("turn_stick_pct") / max(1.0, abs(turn)))
        self._v = v * slower
        if problem := chair.drive(turn * slower, forward * slower):
            self._drop("takeover" if "took over" in problem else "link", problem)
            return "aborted", problem
        return None
