"""Run the chair through one supervised move at a time: a straight, a turn, or a pilot's moves.

    runner = Runner(session)               # the console's Session
    runner.straight(0.76)                  # returns at once; the work happens on a thread
    runner.turn(90)                        # degrees to the left, by the phone's gyro
    runner.pilot("go to the door", pilot)  # a model picks straights and turns; see pilot.py
    runner.gemini("go to the door", pilot) # a model picks drawn paths; see gemini_pilot.py
    runner.route("go to the door", pilot)  # a model draws its route; see route_pilot.py
    runner.state()                         # what it is doing, for the page
    runner.abort("why")                    # stops the chair now

Nothing here moves the chair unless ``chair.armed()`` is true, which only a press of Run on
the bench page makes it, and a run ends the moment that page stops asking after it. Every
way a run can end stops the chair first.

Every move answers with what actually happened, in plain words, for the model that asked
for it (and for the record): what was asked and what was measured ("asked left 25°, gyro
measured left 18°"), how far it is believed to have driven of how far was asked, and, if it
was stopped, exactly why. Only turning is measured (the gyro). Distance is believed: speed
times time, since nothing on the chair measures it. A chair that is pushed but going
nowhere is told by the gyro staying quiet while the camera's picture stops changing.
"""

from __future__ import annotations

import math
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from fsd_ml import candidates
from fsd_ml.chair_link import to_stick
from fsd_ml.gemini_pilot import GeminiPilot
from fsd_ml.pilot import MAX_BACKWARD_M, MAX_FORWARD_M, MAX_TURN_DEG, Call, Pilot
from fsd_ml.pipeline.control import follow
from fsd_ml.route_pilot import RoutePilot

if TYPE_CHECKING:
    from fsd_ml.console.server import Session

# How the chair is driven (stick strengths, ramps, how early a turn lets go) comes from the
# bench's settings, read afresh on every move: see fsd_ml/bench.py.
TURN_TIMEOUT_S_PER_DEG = 0.12
TURN_TICK_S = 0.05
# The turn rate is the change in the gyro's angle over this long.
RATE_WINDOW_S = 0.2
# A turn is over once the chair has turned slower than this for this long.
STILL_DEG_PER_S = 3.0
STILL_S = 0.2
# A stick held this long with nothing to show for it is not moving the chair: a turn that
# has turned less than STUCK_DEG, or a drive with the gyro quiet and the picture changing
# less than PICTURE_STILL (live.LiveFeed.picture_change: about 0.3 standing, 4 or more moving).
STUCK_S = 1.5
STUCK_DEG = 2.0
# A turn that has coasted this far past its goal is left there: pushing back for less only
# overshoots the other way. And it is pushed at most this many times.
OVERSHOOT_DEG = 5.0
MAX_PUSHES = 4
PICTURE_STILL = 1.0
STUCK = f"the stick was held {STUCK_S:g} s but the chair isn't moving: stuck or blocked"
# Holding a heading while driving straight: sideways stick per degree off, more of it per
# degree for every second it stays off (that is what cancels the chair's own steady veer,
# about 1 degree a second to the right), and the most of each. Measured: at 1% per degree
# alone a 0.76 m straight ended 4.5 degrees off at both 6% and 10% forward, having pushed
# no harder than 5%: the casters swing it off in the first half second.
HEADING_PCT_PER_DEG = 2.5
HEADING_PCT_PER_DEG_S = 1.0
HEADING_MAX_PCT = 10.0
HEADING_VEER_MAX_PCT = 3.0
# How long a turn stick is held with no rotation before it is raised (the chair always
# takes a moment to start), and
RAISE_AFTER_S = 0.7
# How fast a stick that is not turning the chair is raised.
RAISE_PCT_PER_S = 6.0


# Longest a straight or backing move may drive, however slow the chair is believed to be.
MAX_MOVE_S = 4.0
# Standing still before each look: longer than a frame stays fresh, so the picture shown is
# from after the move, and sharp.
LOOK_SETTLE_S = 0.6
NO_PICTURE = "no camera picture newer than 0.5 s arrived within 5 s: the phone's stream has stopped"
# Longest one picked path may be driven. Its distance is believed too, but the gyro steers
# it and each pick is meant to cover ground, so it gets longer than a blind straight.
MAX_PATH_S = 12.0


class Runner:
    def __init__(
        self,
        chair: Session,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._chair = chair
        self._clock, self._sleep = clock, sleep
        self._abort: str | None = None
        self._count = 0
        # The move under way, for the record: see _begin and _end.
        self._moves = 0
        self._move: dict[str, Any] | None = None
        # What the last move achieved, as its end was recorded.
        self.achieved: dict[str, Any] = {}
        # The turn stick added, over turn_stick_pct, that the last turn needed to get the
        # chair turning: where the next turn starts from.
        self._turn_boost = 0.0
        self._move_s = 0.0
        self._state: dict[str, Any] = {"running": False, "phase": "idle"}

    def running(self) -> bool:
        return bool(self._state.get("running"))

    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def abort(self, why: str) -> None:
        """Stop whatever is running. Safe to call at any time, from any thread."""
        if self.running():
            self._abort = why

    def turn(self, degrees_left: float, background: bool = True) -> dict[str, Any]:
        return self._start(
            "turn", lambda: self._turn(degrees_left), background, degrees_left=degrees_left
        )

    def straight(self, distance_m: float, background: bool = True) -> dict[str, Any]:
        """Drive straight for a distance. The real distance, measured, calibrates the speed."""
        return self._start(
            "straight", lambda: self._straight(distance_m), background, distance_m=distance_m
        )

    def pilot(
        self, say: str, pilot: Pilot, max_turns: int = 30, background: bool = True
    ) -> dict[str, Any]:
        """Let a model drive: show it the picture, carry out the moves it asks for, repeat.

        The model chooses the moves; this runner carries them out and stops them when it must.
        """
        return self._start("pilot", lambda: self._pilot(pilot, max_turns), background, say=say)

    def gemini(
        self, say: str, pilot: GeminiPilot, max_looks: int = 30, background: bool = True
    ) -> dict[str, Any]:
        """Let a model drive by picking from the paths drawn on the picture, one per look.

        Stop and go: the chair stands still while the model looks and decides.
        """
        return self._start("gemini", lambda: self._gemini(pilot, max_looks), background, say=say)

    def route(
        self, say: str, pilot: RoutePilot, max_looks: int = 30, background: bool = True
    ) -> dict[str, Any]:
        """Let a model drive by drawing its route on the picture: the chair follows it
        without stopping and asks for the next while still driving. See route_run.py."""
        from fsd_ml.route_run import RouteRun

        work = RouteRun(self, pilot, max_looks).run
        return self._start("route", work, background, say=say)

    def _gemini(self, pilot: GeminiPilot, max_looks: int) -> tuple[str, str]:
        start = self._chair.yaw_rad()
        outcome = "none yet: this is the first look."
        for look in range(1, max_looks + 1):
            if why := self._stopped():
                return "aborted", why
            frame, _ = self._look()
            if frame is None:
                return "failed", NO_PICTURE
            paths = self._chair.candidates()
            picture = candidates.draw(frame.jpeg, paths, self._chair.setup)
            heading = math.degrees(self._chair.yaw_rad() - start)
            self._phase(f"look {look}: Gemini is thinking", steps=look)
            choice = pilot.next(picture, candidates.options(paths), outcome, heading)
            # Enough to make a training sample later: the picture, the task, the path picked.
            self._chair.record(
                "gemini",
                frame,
                model={
                    "look": look,
                    "say": pilot.task,
                    "paths": {label: np.round(path, 2).tolist() for label, path in paths.items()},
                    "last_pick": outcome,
                    "heading_deg": round(heading, 1),
                    **pilot.last,
                },
                picture=picture,
            )
            if why := self._stopped():
                return "aborted", why
            scene, note = pilot.last.get("scene") or "", pilot.last.get("note") or ""
            if choice is None:
                return "failed", f"Gemini gave no pick it was offered ({pilot.last.get('choice')})"
            if choice in ("S", "X"):
                return ("arrived" if choice == "S" else "failed"), f"Gemini said: {scene} {note}"
            words = candidates.options(paths)[choice]
            # ``call`` rides along in every command recorded while the pick is driven.
            self._phase(
                f"look {look}: {words} · {note}", steps=look, call={"look": look, "choice": choice}
            )
            if choice in candidates.TURNS:
                result, text = self._turn(candidates.TURNS[choice])
            else:
                result, text = self._follow(paths[choice])
            self._phase(self._state["phase"], call=None)
            if result == "aborted":
                return result, text
            outcome = f"{choice} ({words}). What happened: {text}."
        return "failed", f"not done after {max_looks} looks"

    def _follow(self, path_xy: np.ndarray) -> tuple[str, str]:
        """Drive along a path laid on the floor from where the chair stands now.

        The heading is measured (the gyro); how far it has gone is believed (speed x time, as
        in a straight). Each tick the rest of the path is seen from where the chair is thought
        to be, and the path follower (pipeline/control.py) steers for it, more slowly where
        the curve is tight. It ends when the path's length is believed driven; it stops for an
        obstacle on what is left of the path (the same rule the path was offered by) or a
        chair that is not moving, and never drives longer than MAX_PATH_S.
        """
        path = np.asarray(path_xy, dtype=np.float64).reshape(-1, 2)
        along = np.cumsum(np.linalg.norm(np.diff(path, axis=0, prepend=[[0.0, 0.0]]), axis=1))
        length = float(along[-1])
        self._begin("path", length_m=round(length, 2), path=np.round(path, 2).tolist())
        yaw, x, y, gone, v = self._chair.yaw_rad(), 0.0, 0.0, 0.0, 0.0

        def ended(reason: str, outcome: str, why: str = "", at_m: float | None = None) -> tuple:
            turned = math.degrees(self._chair.yaw_rad() - yaw)
            driven = gone if at_m is None else at_m
            return self._end(
                reason,
                outcome,
                _drove(driven, length, why) + f"; gyro measured {_degrees(turned)}",
                believed_m=round(driven, 2),
                turned_deg=round(turned, 1),
                believed_end_xy=[round(x, 2), round(y, 2)],
            )

        if not self._chair.gyro_fresh():
            return ended("stale_gyro", "failed", "the gyro is not reporting, so it did not start")
        stick = self._chair.setting("drive_stick_pct")
        ramp = max(0.0, self._chair.setting("ramp_s"))
        speed = self._chair.speed_for(stick)
        if speed <= 0:
            return ended("no_speed", "failed", "no speed is known for that stick")
        started = last = self._clock()
        was, quiet = 0.0, None
        while (now := self._clock() - started) < MAX_PATH_S and length - gone > 0.02:
            heading = self._chair.yaw_rad() - yaw
            tick = self._clock() - last
            step, last = v * tick, self._clock()
            x, y, gone = x + step * math.cos(heading), y + step * math.sin(heading), gone + step
            # What is left of the path, as seen from the chair now.
            at = int(np.searchsorted(along, min(gone, length)))
            ahead, left = (path[max(0, at - 1) :] - [x, y]).T
            rest = np.column_stack(
                [
                    ahead * math.cos(heading) + left * math.sin(heading),
                    -ahead * math.sin(heading) + left * math.cos(heading),
                ]
            )
            if why := self._stopped():
                return ended(_code(why), "aborted", why)
            if why := self._chair.blocked(v, rest):
                self._chair.hold()
                return ended(_blocked_code(why), "failed", why)
            if not self._chair.gyro_fresh():
                self._chair.hold()
                return ended("stale_gyro", "failed", "the gyro stopped reporting")
            quiet = self._quiet(quiet, gone, v >= speed / 2, heading - was, tick)
            was = heading
            if quiet and last - quiet[0] >= STUCK_S:
                self._chair.hold()
                return ended("stuck", "failed", STUCK, quiet[1])
            # Eased in over the ramp time, and out over the ground that time covers.
            eased = min(1.0, now / ramp, (length - gone) / (speed * ramp)) if ramp > 0 else 1.0
            v, w = follow(rest, max(eased, 1.0 / stick), speed_limit_mps=speed)
            if length - gone < 0.3:
                w = 0.0  # too near the end to aim at it: the aim swings wide as it closes
            turn, forward = to_stick(
                v, w, self._chair.setting("v_full_mps"), self._chair.setting("w_full_radps")
            )
            # Never a harder turn than a turn on the spot is given: a curve too tight for
            # that at this speed is driven more slowly, not wider.
            slower = min(1.0, self._chair.setting("turn_stick_pct") / max(1.0, abs(turn)))
            turn, forward, v = turn * slower, forward * slower, v * slower
            if problem := self._chair.drive(turn, forward):
                return ended(_code(problem), "aborted", problem)
            self._sleep(0.1)
        self._chair.hold()
        if length - gone > 0.1:
            return ended(
                "time_cap", "finished", f"the {MAX_PATH_S:.0f} s allowed for one path ran out"
            )
        return ended("finished", "finished")

    def _look(self) -> tuple[Any, str]:
        """Stand still, then a fresh camera frame and the obstacle report; no frame if none came.

        A frame counts as fresh for half a second (live.MAX_FRAME_AGE_S), so standing longer
        than that first means the picture was taken after the last move ended.
        """
        self._settle(LOOK_SETTLE_S)
        frame, report = self._chair.observe()
        waited = 0.0
        while frame is None and waited < 5.0 and not self._stopped():
            self._sleep(0.25)
            waited += 0.25
            frame, report = self._chair.observe()
        return frame, report

    def _pilot(self, pilot: Pilot, max_turns: int) -> tuple[str, str]:
        results: list[tuple[str, str, bool]] | None = None
        for turn in range(1, max_turns + 1):
            if why := self._stopped():
                return "aborted", why
            frame, report = self._look()
            if frame is None:
                return "failed", NO_PICTURE
            self._phase(f"look {turn}: the pilot is thinking", steps=turn)
            calls = pilot.next(frame.jpeg, report, results)
            self._chair.record(
                "pilot",
                frame,
                model={
                    "look": turn,
                    "report": report,
                    "calls": [{"name": c.name, **c.input} for c in calls],
                    **pilot.last,
                },
            )
            if why := self._stopped():
                return "aborted", why
            if not calls:
                said = (pilot.last.get("text") or "")[:160]
                return "failed", f"the pilot gave no move ({pilot.last.get('stop_reason')}) {said}"
            results, cut_short = [], False
            for call in calls:
                if call.name == "done":
                    outcome = "arrived" if call.input.get("arrived") else "failed"
                    return outcome, f"the pilot said: {call.why}"
                if cut_short:
                    results.append(
                        (call.id, "Not run: an earlier move in this reply was stopped.", True)
                    )
                    continue
                # ``call`` rides along in every command recorded while the move is under way.
                self._phase(
                    f"look {turn}: {call.says()} · {call.why}",
                    steps=turn,
                    call={"name": call.name, **call.input},
                )
                outcome, text = self._carry_out(call)
                self._phase(self._state["phase"], call=None)
                if outcome == "aborted":
                    return outcome, text
                cut_short = outcome == "failed"
                results.append((call.id, text, cut_short))
        return "failed", f"not done after {max_turns} looks"

    def _carry_out(self, call: Call) -> tuple[str, str]:
        """One move the pilot asked for, cut to what is allowed.

        Returns "ok", "failed" or "aborted", and what to tell the pilot about it.
        """
        amount = call.input.get("distance_m" if call.name.startswith("move_") else "degrees")
        if call.name not in ("move_forward", "move_backward", "turn_left", "turn_right"):
            self._chair.note("refused", call=call.name, why="no such tool")
            return "failed", f"There is no tool called {call.name}."
        if not isinstance(amount, int | float) or not math.isfinite(amount) or amount <= 0:
            # A negative amount is not "the other way": the model names the direction by tool.
            self._chair.note("refused", call=call.name, why=f"{amount!r} is not a positive amount")
            return "failed", f"Not run: {amount!r} is not a positive amount."
        if call.name.startswith("move_"):
            forward = call.name == "move_forward"
            allowed, unit = (
                max(0.05, min(MAX_FORWARD_M if forward else MAX_BACKWARD_M, amount)),
                "m",
            )
            outcome, text = self._straight(allowed) if forward else self._back(allowed)
        else:
            allowed, unit = max(3.0, min(MAX_TURN_DEG, amount)), "°"
            outcome, text = self._turn(allowed if call.name == "turn_left" else -allowed)
        if amount != allowed:
            text += f" (you asked for {amount:g} {unit}; {allowed:g} {unit} is what is allowed)"
        return {"finished": "ok"}.get(outcome, outcome), f"{text[0].upper()}{text[1:]}."

    def _settle(self, seconds: float | None = None) -> None:
        """Stand still for a moment so one move does not run into the next."""
        self._chair.hold()
        until = self._clock() + (self._chair.setting("settle_s") if seconds is None else seconds)
        while self._clock() < until and not self._stopped():
            self._sleep(0.1)

    def _drive_for(self, distance_m: float, forward: bool) -> tuple[str, str]:
        """Drive straight for the time a distance takes, eased in and out.

        Nothing measures how far the chair goes: it drives for the time the believed speed
        says. The heading it set off on is held by the gyro, with a little sideways stick in
        proportion to how far off it is (the chair veers by itself). Going forward it stops
        for whatever the obstacle sense finds ahead; nothing sees behind the chair, so
        backing is kept short by its caller. It gives up if the chair is not moving.
        """
        way = "forward" if forward else "back"
        self._begin("straight" if forward else "back", distance_m=round(distance_m, 3))
        heading = was = self._chair.yaw_rad()
        gone = 0.0

        def ended(reason: str, outcome: str, why: str = "", at_m: float | None = None) -> tuple:
            driven = gone if at_m is None else at_m
            return self._end(
                reason,
                outcome,
                _drove(driven, distance_m, why, way),
                believed_m=round(driven, 3),
                turned_deg=round(math.degrees(self._chair.yaw_rad() - heading), 1),
            )

        stick = self._chair.setting("drive_stick_pct")
        ramp = max(0.0, self._chair.setting("ramp_s"))
        speed = self._chair.speed_for(stick)
        if speed <= 0:
            return ended("no_speed", "failed", "no speed is known for that stick")
        # Easing in and out each cover half the ground of the same time at full stick. The
        # believed speed is a guess (nothing measures distance), so a move never runs past
        # MAX_MOVE_S whatever it says.
        seconds, started = min(distance_m / speed + ramp, MAX_MOVE_S), self._clock()
        y, last, quiet, veer = 0.0, started, None, 0.0
        while (now := self._clock() - started) < seconds:
            tick = self._clock() - last
            gone += self._chair.speed_for(y) * tick
            last = self._clock()
            if why := self._stopped():
                return ended(_code(why), "aborted", why)
            if forward and (why := self._chair.blocked(self._chair.speed_for(y))):
                self._chair.hold()
                return ended(_blocked_code(why), "failed", why)
            yaw = self._chair.yaw_rad()
            quiet = self._quiet(quiet, gone, y >= stick / 2, yaw - was, tick)
            was = yaw
            if quiet and last - quiet[0] >= STUCK_S:
                self._chair.hold()
                return ended("stuck", "failed", STUCK, quiet[1])
            # Once the chair is moving, a small sideways stick is a small, smooth turn
            # (hardware/wheelchair.py adds the turn dead zone only when standing), so the
            # heading can be held in proportion. Without the gyro the stick stays straight.
            off = math.degrees(heading - yaw) if self._chair.gyro_fresh() else 0.0
            veer += HEADING_PCT_PER_DEG_S * off * tick
            veer = max(-HEADING_VEER_MAX_PCT, min(HEADING_VEER_MAX_PCT, veer))
            x = -max(-HEADING_MAX_PCT, min(HEADING_MAX_PCT, HEADING_PCT_PER_DEG * off + veer))
            eased = min(1.0, now / ramp, (seconds - now) / ramp) if ramp > 0 else 1.0
            y = max(1.0, stick * eased)
            if problem := self._chair.drive(x, y if forward else -y, reverse=not forward):
                return ended(_code(problem), "aborted", problem)
            self._sleep(0.1)
        gone += self._chair.speed_for(y) * (self._clock() - last)
        self._chair.hold()
        if distance_m - gone > 0.05:
            return ended(
                "time_cap", "finished", f"the {MAX_MOVE_S:.0f} s allowed for one move ran out"
            )
        return ended("finished", "finished")

    def _quiet(
        self,
        since: tuple[float, float] | None,
        gone_m: float,
        pushing: bool,
        turned_rad: float,
        tick_s: float,
    ) -> tuple[float, float] | None:
        """Since when (and from how far along) a pushed chair has shown no sign of moving:
        the gyro quiet and the picture not changing. None while it moves, while the stick is
        still easing in, and when there is no picture to compare, since then it cannot be told.
        """
        change = self._chair.picture_change()
        turning = tick_s > 0 and abs(math.degrees(turned_rad)) / tick_s >= STILL_DEG_PER_S
        if not pushing or turning or change is None or change >= PICTURE_STILL:
            return None
        return since or (self._clock(), gone_m)

    def _straight(self, distance_m: float) -> tuple[str, str]:
        return self._drive_for(distance_m, forward=True)

    def _back(self, distance_m: float) -> tuple[str, str]:
        return self._drive_for(distance_m, forward=False)

    def _start(
        self, kind: str, work: Callable[[], tuple[str, str]], background: bool, **asked: Any
    ) -> dict[str, Any]:
        if self.running():
            raise RuntimeError("something is already running")
        if not self._chair.armed():
            raise RuntimeError("not armed: runs are started from the bench page")
        self._count += 1
        self._abort = None
        self._state = {
            "running": True,
            "id": self._count,
            "kind": kind,
            **asked,
            "phase": "starting",
            "steps": 0,
        }

        def run() -> None:
            started = self._clock()
            problem = self._chair.begin()
            try:
                outcome, reason = ("failed", problem) if problem else work()
            except Exception as error:  # noqa: BLE001  whatever goes wrong, the chair must stop
                outcome, reason = "failed", f"error: {error}"
                if self._move:
                    self._end("exception", outcome, reason)
                self._chair.note("exception", error=repr(error), trace=traceback.format_exc())
            finally:
                self._chair.end()
            self._state = {
                **self._state,
                "running": False,
                "phase": "done",
                "call": None,
                "outcome": outcome,
                "reason": reason,
                "seconds": round(self._clock() - started, 1),
            }
            self._chair.finished(self._state)

        if background:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()
        return self.state()

    def _stopped(self) -> str | None:
        """Why the run must end now, if it must."""
        if self._abort:
            return self._abort
        if not self._chair.armed():
            return DISARMED
        if not self._chair.page_alive():
            return PAGE_GONE
        return None

    def _phase(self, phase: str, **more: Any) -> None:
        self._state = {**self._state, "phase": phase, **more}

    def _begin(self, kind: str, **planned: Any) -> None:
        """Mark the start of one move in the record: what it is and what is planned."""
        self._moves += 1
        self._move = {"action": self._moves, "kind": kind, "planned": planned}
        self._move_s = self._clock()
        self._phase(self._state.get("phase", ""), action=self._moves)
        self._chair.note("action_start", **self._move)

    def _end(self, reason: str, outcome: str, text: str, **achieved: Any) -> tuple[str, str]:
        """Mark the end of the move begun: what was achieved and exactly why it ended.

        Every way out of a move goes through here, so none ends without a record. ``reason``
        is one of: finished, replaced, stalled, time_cap, obstacle, no_floor, stuck,
        stale_gyro, no_speed, takeover, link, heartbeat, disarmed, stopped, exception.
        """
        move, self._move = self._move or {}, None
        self.achieved = achieved
        self._phase(self._state.get("phase", ""), action=None)
        self._chair.note(
            "action_end",
            **move,
            achieved=achieved,
            seconds=round(self._clock() - self._move_s, 2),
            outcome=outcome,
            ended=reason,
            detail=text,
        )
        return outcome, text

    def _turn(self, degrees_left: float) -> tuple[str, str]:
        """Rotate on the spot by an angle, by the gyro. Positive is to the left.

        The chair keeps turning for a moment after the stick lets go. So it is pushed one
        way (full ``turn_stick_pct``, easing toward ``turn_min_pct`` over the last
        ``turn_slow_deg``) until its angle plus its turn rate times ``turn_coast_s`` reaches
        the goal, then let go and left to coast until it stands still. Only then is it judged:
        within ``turn_done_deg``, or past the goal by no more than OVERSHOOT_DEG, it is done;
        otherwise it is pushed again toward the goal, letting go by how far it really coasted
        the last time. The stick never changes side while the chair is moving, so it cannot
        hunt back and forth. A stick that is not turning the chair is raised little by
        little, and given up on after STUCK_S.

        Whatever ends it, what is said is what was asked and what the gyro measured.
        """
        asked = _degrees(degrees_left)
        self._begin("turn", asked_deg=degrees_left)
        start = self._chair.yaw_rad()
        pushes, top = 1, 0.0

        def ended(reason: str, outcome: str, why: str = "") -> tuple[str, str]:
            got = math.degrees(self._chair.yaw_rad() - start)
            return self._end(
                reason,
                outcome,
                f"asked {asked}, gyro measured {_degrees(got)}{why}",
                got_deg=round(got, 1),
                pushes=pushes,
                top_stick_pct=round(top, 1),
            )

        if not self._chair.gyro_fresh():
            return ended("stale_gyro", "failed", ": the gyro is not reporting, so it did not turn")
        goal = start + math.radians(degrees_left)
        cap_s = 6.0 + abs(degrees_left) * TURN_TIMEOUT_S_PER_DEG
        most = self._chair.setting("turn_stick_pct")
        least = min(most, self._chair.setting("turn_min_pct"))
        hardest = max(most, self._chair.setting("turn_max_pct"))
        slow = max(1.0, self._chair.setting("turn_slow_deg"))
        close_enough = self._chair.setting("turn_done_deg")
        coast_s = self._chair.setting("turn_coast_s")
        if self._state.get("kind") == "turn":
            self._phase("turning")
        began = self._clock()
        # (when, yaw) over the last RATE_WINDOW_S, for the turn rate.
        seen: deque[tuple[float, float]] = deque()
        # The side being pushed (+1 left), and the push under way: when it began and the yaw
        # then; None while coasting. ``raised`` is the stick added because it was not turning.
        way = math.copysign(1.0, degrees_left)
        pushing: tuple[float, float] | None = (began, start)
        raised, still_since = self._turn_boost, None
        # The yaw and turn rate when the stick last let go, to learn the coast from.
        let_go = (start, 0.0)
        while True:
            now, yaw = self._clock(), self._chair.yaw_rad()
            if why := self._stopped():
                return ended(_code(why), "aborted", f": {why}")
            if not self._chair.gyro_fresh():
                self._chair.hold()
                return ended("stale_gyro", "failed", ": the gyro stopped reporting")
            if now - began > cap_s:
                self._chair.hold()
                return ended("time_cap", "failed", f": the {cap_s:.0f} s allowed for it ran out")
            seen.append((now, yaw))
            while now - seen[0][0] > RATE_WINDOW_S:
                seen.popleft()
            rate = math.degrees(yaw - seen[0][1]) / (now - seen[0][0]) if now > seen[0][0] else 0.0
            error = math.degrees(goal - yaw)
            if pushing is None:
                # Coasting. Judge it only once it stands still.
                if abs(rate) >= STILL_DEG_PER_S:
                    still_since = None
                elif still_since is None:
                    still_since = now
                elif now - still_since >= STILL_S:
                    if abs(let_go[1]) > 5.0:  # how long a coast it really was, for the next
                        coasted = math.degrees(yaw - let_go[0]) / let_go[1]
                        coast_s = max(0.2, min(1.5, coasted))
                    past = -way * error  # how far past the goal, the way it was pushed
                    if abs(error) <= close_enough or 0 < past <= OVERSHOOT_DEG:
                        break
                    if pushes >= MAX_PUSHES:
                        return ended("time_cap", "failed", f": still off after {pushes} pushes")
                    way, pushing = math.copysign(1.0, error), (now, yaw)
                    pushes += 1
            else:
                # Where it would end up if let go now, short of the goal by this much.
                to_go = way * (error - rate * coast_s)
                if to_go <= 0:
                    self._chair.hold()
                    pushing, still_since, let_go = None, None, (yaw, rate)
                else:
                    # From rest the casters may be trailing the wrong way, and the usual
                    # stick does not always break them free: one that is not turning the
                    # chair is raised, up to turn_max_pct, before it is called stuck. What it
                    # took is kept for the rest of the turn (dropping back would only stall
                    # it again) and most of it for the next turn.
                    held = now - pushing[0]
                    if abs(rate) < STILL_DEG_PER_S and held > RAISE_AFTER_S:
                        raised = min(hardest - least, raised + RAISE_PCT_PER_S * TURN_TICK_S)
                    stick = min(hardest, min(most, max(least, most * to_go / slow)) + raised)
                    if held >= STUCK_S:
                        if abs(math.degrees(yaw - pushing[1])) >= STUCK_DEG:
                            pushing = (now, yaw)
                        elif stick >= hardest - 0.01 and held >= STUCK_S + RAISE_AFTER_S + 1.0:
                            self._chair.hold()
                            return ended(
                                "stuck",
                                "failed",
                                f": {STUCK} (up to {top:.0f}% stick, the gyro shows no rotation)",
                            )
                    top = max(top, stick)
                    # Stick right is positive; a left turn is stick left.
                    if problem := self._chair.drive(-way * stick, 0.0):
                        return ended(_code(problem), "aborted", f": {problem}")
            self._sleep(TURN_TICK_S)
        self._turn_boost = 0.8 * raised
        return ended("finished", "finished")


DISARMED = "disarmed"
PAGE_GONE = "the bench page stopped watching (closed, or not answering)"


def _code(why: str) -> str:
    """Why a run was stopped from outside, or a stick was not sent, as a word for the record."""
    if why == DISARMED:
        return "disarmed"
    if why == PAGE_GONE:
        return "heartbeat"
    if "took over" in why:
        return "takeover"
    return "link" if "Wi-Fi" in why else "stopped"


def _blocked_code(why: str) -> str:
    return "obstacle" if why.startswith("obstacle") else "no_floor"


def _drove(gone_m: float, asked_m: float, stopped: str = "", way: str = "") -> str:
    """How a straight or a path went: believed distance of what was asked, and why it ended."""
    said = f"drove {way + ' ' if way else ''}about {gone_m:.2f} m of {asked_m:.2f} m (believed)"
    return f"{said}, then stopped: {stopped}" if stopped else said


def _degrees(left: float) -> str:
    """An angle to the left in words: "left 25°"."""
    return f"{'left' if left >= 0 else 'right'} {abs(left):.0f}°"
