"""Carry out a route pilot's run: drive what it draws, without stopping to think.

    RouteRun(runner, pilot, max_looks).run()   # called by moves.Runner.route

The pilot (fsd_ml/route_pilot.py) is slow, a few seconds a look, and the chair should not
stand waiting for it. So the two go on side by side:

- The chair follows the route it has. Where it is, is kept as a believed pose in the run's
  own frame: heading from the gyro, distance from the calibrated speed of the stick sent.
- As soon as it has set off on a route (REPLAN_M), the next look is asked for: the picture as it is
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
from fsd_ml.chair_link import to_stick
from fsd_ml.moves import STUCK, STUCK_S, _degrees
from fsd_ml.pipeline.control import follow
from fsd_ml.pipeline.footprint import SAMPLE_SPACING_M
from fsd_ml.route_pilot import Plan, Pose, Remembered, RoutePilot, into, out_of

if TYPE_CHECKING:
    from fsd_ml.moves import Runner

TICK_S = 0.1
# The next look is asked for once this much of the route in hand has been driven.
REPLAN_M = 0.1
# While driving, a look is asked for at least this often, however little ground is covered.
REASK_S = 6.0
# A route in hand that the chair has not been driven along for this long is given up.
STALLED_S = 2.0
# How far the harness backs off to get itself room, and how many times it tries that for
# one route before asking the model for another way.
BACK_OFF_M = 0.3
MAX_BACK_OFFS = 2
# How many times a route that the chair had to stop on is fitted again before the model is asked.
MAX_REFITS = 2
# This near the end of a route, being stopped by the stop rule is just reaching the end.
ENDING_M = 0.2
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
        # The run's frame is where the chair stands and faces as the run begins: the gyro's
        # own angle counts from when the phone connected, so it is taken off.
        self._yaw0 = self._chair.yaw_rad()
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
        self._was_yaw = 0.0
        # The look being asked, if any, and where the chair was when the last one was taken.
        self._asking: threading.Thread | None = None
        self._answer: dict[str, Any] | None = None
        self._asked_xy = (0.0, 0.0)
        self._asked_s = self._clock()
        # When the chair was last being driven forward along the route in hand.
        self._driving_s = self._clock()
        self._looks = 0
        self._bad = 0
        # Routes in a row of which nothing could be driven, and what the model answered.
        self._wanted: np.ndarray | None = None
        self._refits = 0
        self._no_picture_since: float | None = None
        # What the model is given back: its notes, what has happened since its last answer,
        # and where it last marked the goal (run's frame).
        self._notes: list[str] = []
        self._happened: list[str] = []
        self._goal: tuple[float, float] | None = None
        self._remembered = Remembered(self._setup, (0.0, 0.0, 0.0))
        self._seen_s = 0.0

    def _heading(self) -> float:
        return self._chair.yaw_rad() - self._yaw0

    def _pose(self) -> Pose:
        return (self._xy[0], self._xy[1], self._heading())

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
        yaw = self._heading()
        step = self._v * (now - self._at_s)
        self._at_s = now
        self._driven += abs(step)
        self._axle[0] += step * math.cos(yaw)
        self._axle[1] += step * math.sin(yaw)
        self._xy[0] = self._axle[0] + self._pivot * math.cos(yaw)
        self._xy[1] = self._axle[1] + self._pivot * math.sin(yaw)
        self._poses.append((now, self._pose()))
        self._remembered.stand(self._pose())

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
        elif math.dist(self._xy, self._asked_xy) < REPLAN_M and now - self._asked_s < REASK_S:
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
        pose = self._pose_at(now - frame.age_s(now))
        moving = "The chair is standing still."
        if self._path is not None:
            moving = "The chair is driving your last route, and keeps to it until you answer."
        goal = None if self._goal is None else tuple(into(pose, [self._goal])[0])
        self._looks += 1
        happened, self._happened = self._happened, []
        self._asked_xy, self._asked_s = (self._xy[0], self._xy[1]), now
        self._runner._phase(f"look {self._looks}: the pilot is thinking", steps=self._looks)
        look = {"look": self._looks, "frame": frame, "pose": pose}
        notes, half_view = list(self._notes), self._setup.camera.hfov_deg / 2

        def ask() -> None:
            try:
                ground = self._chair.ground(frame)
                behind = goal is not None and route_pilot.hidden(goal, ground, self._setup)
                said = route_pilot.report(
                    look["look"], moving, happened, pose, goal, notes, half_view, behind
                )
                shown = route_pilot.picture(frame.jpeg, self._setup, None if behind else goal)
                plan = self._pilot.next(shown, said)
                self._answer = {**look, "shown": shown, "plan": plan, "ground": ground}
            except Exception as error:  # noqa: BLE001  a failed look must not end the driving
                self._answer = {**look, "error": repr(error)}

        self._asking = threading.Thread(target=ask, daemon=True)
        self._asking.start()
        return None

    def _fit(self, kept: list[tuple[float, float]], pose: Pose) -> np.ndarray:
        """The path for a route through ``kept`` (chair's frame at ``pose``), fitted past
        everything the run has seen."""
        return route_pilot.fit(
            kept,
            self._remembered.around(pose, self._driven),
            self._setup,
            self._chair.setting("path_clearance_m"),
        )

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
        kept: list[tuple[float, float]] = []
        path = np.empty((0, 2))
        if plan.action == "route":
            points, kept = route_pilot.read(plan, ground, self._setup)
            path = self._fit(kept, pose)
            record["route"] = {
                "points": [[round(a, 2), round(b, 2), floor] for a, b, floor in points],
                "path": np.round(path, 2).tolist(),
            }
        self._chair.record("route", answer["frame"], model=record, picture=answer["shown"])
        self._runner._phase(f"look {answer['look']}: {plan.action} · {plan.note}")
        if plan.action in ("arrived", "give_up"):
            self._drop("replaced", f"the pilot answered {plan.action}")
            outcome = "arrived" if plan.action == "arrived" else "failed"
            return outcome, f"the pilot said: {plan.scene} {plan.note}"
        if plan.action == "route":
            wanted = out_of(pose, kept) if kept else None
            if len(path):
                self._wanted, self._refits = wanted, 0
                return self._adopt(out_of(pose, path))
            if self._path is not None:
                return None  # nothing new to drive: the chair keeps to the route in hand
            return self._another_way(wanted)
        self._drop("replaced", f"the pilot answered {plan.action}")
        asked = route_pilot.TURNS[plan.action]
        outcome, text = self._turn(asked)
        if outcome == "failed" and "did not turn" in text and self._back_off():
            outcome, text = self._turn(asked)  # the harness's own second try, with more room
        self._move_on(self._clock())
        self._stood_since = self._clock()
        if outcome == "aborted":
            return "aborted", text
        side = "left" if asked > 0 else "right"
        turned = math.degrees(self._heading() - pose[2])
        self._happened.append(
            f"The chair could not turn {side} from where it stands."
            if outcome == "failed" and "did not turn" in text
            else f"The chair turned {abs(turned):.0f}° to the {'left' if turned > 0 else 'right'}."
        )
        return None

    def _back_off(self) -> bool:
        """Back straight up a little, if the floor behind is known to be clear. This is the
        harness getting itself room, not something the model is asked about."""
        if not self._remembered.room_to_back(self._pose(), BACK_OFF_M, self._driven):
            return False
        outcome, _ = self._runner._back(BACK_OFF_M)
        backed = float(self._runner.achieved.get("believed_m", 0.0))
        yaw = self._heading()
        self._axle[0] -= backed * math.cos(yaw)
        self._axle[1] -= backed * math.sin(yaw)
        self._move_on(self._clock())
        self._stood_since = self._clock()
        return outcome == "finished"

    def _another_way(self, wanted: np.ndarray | None) -> tuple[str, str] | None:
        """No path could be fitted to the route the model drew from where the chair stands.
        Before asking it again, the harness tries for itself: back off, and fit the same
        route from there, up to MAX_BACK_OFFS times."""
        for _ in range(MAX_BACK_OFFS if wanted is not None else 0):
            if not self._back_off():
                break
            if self._runner._stopped():
                return None
            here = self._pose()
            kept = [tuple(point) for point in into(here, wanted) if point[0] > 0]
            path = self._fit(kept, here)
            if len(path):
                return self._adopt(out_of(here, path))
        self._happened.append(
            "The chair found no way to drive the route you drew. Try another way."
        )
        return None

    def _adopt(self, path: np.ndarray) -> tuple[str, str] | None:
        """Take up a new path (run's frame) from wherever on it the chair has got to."""
        here = self._pose()
        gaps = np.linalg.norm(path - self._xy, axis=1)
        nearest = int(np.argmin(gaps))
        ahead = into(here, path[nearest : nearest + 6])
        if len(ahead) < 2:
            return None  # the chair has already driven past its end
        # Which way the route runs from where the chair joins it, against the chair's heading.
        to, start = ahead[min(len(ahead) - 1, 4)], (0.0, 0.0) if nearest == 0 else ahead[0]
        bearing = math.degrees(math.atan2(to[1] - start[1], to[0] - start[0]))
        joins = self._path is not None and gaps[nearest] <= JOIN_M and abs(bearing) <= JOIN_DEG
        if joins:
            self._end_route("replaced", "finished")  # and drive on, onto the new one
        else:
            self._drop("replaced", "the new route starts away from where the chair was heading")
            if abs(bearing) > PIVOT_DEG:
                outcome, text = self._turn(bearing)
                self._move_on(self._clock())
                self._stood_since = self._clock()
                if outcome == "aborted":
                    return "aborted", text
                if outcome == "failed":
                    side = "left" if bearing > 0 else "right"
                    self._happened.append(
                        f"The chair could not turn {side} to face the route you drew."
                    )
                    return None
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
        room, why = self._remembered.turn_room(self._pose(), degrees_left, self._driven)
        if abs(room) < min(MIN_TURN_DEG, abs(degrees_left)):
            self._chair.note("refused", call="turn", asked_deg=degrees_left, room_deg=room, why=why)
            return "failed", f"asked {_degrees(degrees_left)}, did not turn: {why}"
        outcome, text = self._runner._turn(room)
        if room != degrees_left:
            text += f" (you asked for {_degrees(degrees_left)}; further round, {why})"
        return outcome, text

    def _end_route(self, reason: str, outcome: str, why: str = "") -> str:
        """Close the record of the route in hand, with exactly why it ended; and give the
        few words of it the model is told."""
        driven = math.dist(self._xy, self._started_xy)
        left_m = (
            max(0, len(self._path) - self._along) * SAMPLE_SPACING_M
            if self._path is not None
            else 0.0
        )
        text = f"drove about {driven:.1f} m along the route ({left_m:.1f} m of it was left)"
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
        return f"drove {driven:.1f} m along your route"

    def _drop(self, reason: str, why: str) -> None:
        """Stop driving the route in hand, if there is one, and stand."""
        if self._path is not None:
            self._chair.hold()
            outcome = {"stopped": "aborted", "replaced": "finished"}.get(reason, "failed")
            text = self._end_route(reason, outcome, why)
            if reason != "replaced":
                self._happened.append(f"The chair {text}, then had to stop.")
        self._v, self._moving_since = 0.0, None
        self._stood_since = self._clock()

    def _stopped_short(self, reason: str, why: str) -> tuple[str, str] | None:
        """The chair has had to stop on its route. Before the model is asked, the harness
        looks for a way on by itself: the same route fitted again from here, then from a
        little further back."""
        wanted, self._refits = self._wanted, self._refits + 1
        self._drop(reason, why)
        if wanted is None or self._refits > MAX_REFITS:
            return None
        here = self._pose()
        kept = [tuple(point) for point in into(here, wanted) if point[0] > 0]
        path = self._fit(kept, here)
        if len(path):
            return self._adopt(out_of(here, path))
        return self._another_way(wanted)

    def _drive(self, now: float) -> tuple[str, str] | None:
        """One tick of following the route in hand."""
        chair = self._chair
        here = self._pose()
        # How far along the route the chair has got: the nearest point not behind where it was.
        window = self._path[self._along : self._along + 15]
        self._along += int(np.argmin(np.linalg.norm(window - self._xy, axis=1)))
        rest = into(here, self._path[self._along + 1 :])
        # The route is done when none of it is left, or what is left is no longer in front
        # of the chair: it has drawn level with the end a little to one side.
        if len(rest) < 1 or (len(rest) * SAMPLE_SPACING_M < 0.4 and rest[-1][0] <= 0.05):
            chair.hold()
            self._happened.append(f"The chair {self._end_route('finished', 'finished')}.")
            self._v, self._moving_since, self._stood_since = 0.0, None, now
            return None
        # Nothing may wait for ever: a route the chair is not being driven along is given
        # up, said so, and looked at afresh.
        if self._v > 0.01 or self._moving_since is None:
            self._driving_s = now
        elif now - self._driving_s > STALLED_S:
            self._drop(
                "stalled",
                f"the chair had not been driven along it for {STALLED_S:.0f} s (what was left"
                " of it was beside or behind the chair)",
            )
            return None
        ahead = into(here, self._path[self._along :])
        # The stop: only when the body is about to touch something on what is left of the
        # route. At the very end of a route that is simply the end of it (a route is fitted
        # right up to what it meets), not a stop to recover from.
        if why := chair.blocked(self._v, ahead, self._remembered.around(here, self._driven)):
            if len(rest) * SAMPLE_SPACING_M <= ENDING_M:
                chair.hold()
                self._happened.append(f"The chair {self._end_route('finished', 'finished')}.")
                self._v, self._moving_since, self._stood_since = 0.0, None, now
                return None
            kind = "obstacle" if why.startswith("obstacle") else "no_floor"
            return self._stopped_short(kind, why)
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
        # Never more sideways stick than curve_stick_pct: a curve too tight for that at this
        # speed is driven more slowly, not wider.
        slower = min(1.0, chair.setting("curve_stick_pct") / max(1.0, abs(turn)))
        self._v = v * slower
        if problem := chair.drive(turn * slower, forward * slower):
            self._drop("takeover" if "took over" in problem else "link", problem)
            return "aborted", problem
        return None
