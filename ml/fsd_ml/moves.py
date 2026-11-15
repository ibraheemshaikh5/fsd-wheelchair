"""Run the chair through one supervised move at a time: a straight, a turn, or a pilot's moves.

    runner = Runner(session)               # the console's Session
    runner.straight(0.76)                  # returns at once; the work happens on a thread
    runner.turn(90)                        # degrees to the left, by the phone's gyro
    runner.pilot("go to the door", pilot)  # a model picks straights and turns; see pilot.py
    runner.gemini("go to the door", pilot) # a model picks drawn paths; see gemini_pilot.py
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
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from fsd_ml import candidates
from fsd_ml.chair_link import to_stick
from fsd_ml.gemini_pilot import GeminiPilot
from fsd_ml.pilot import MAX_BACKWARD_M, MAX_FORWARD_M, MAX_TURN_DEG, Call, Pilot
from fsd_ml.pipeline.control import follow

if TYPE_CHECKING:
    from fsd_ml.console.server import Session

# How the chair is driven (stick strengths, ramps, how early a turn lets go) comes from the
# bench's settings, read afresh on every move: see fsd_ml/bench.py.
TURN_TIMEOUT_S_PER_DEG = 0.12
TURN_TICK_S = 0.05
# The turn rate is the change in the gyro's angle over this long.
RATE_WINDOW_S = 0.2
# A turn is over once the chair has turned slower than this for this long.
STILL_DEG_PER_S = 1.5
STILL_S = 0.3
# A stick held this long with nothing to show for it is not moving the chair: a turn that
# has turned less than STUCK_DEG, or a drive with the gyro quiet and the picture changing
# less than PICTURE_STILL (live.LiveFeed.picture_change: about 0.3 standing, 4 or more moving).
STUCK_S = 1.5
STUCK_DEG = 2.0
PICTURE_STILL = 1.0
STUCK = f"the stick was held {STUCK_S:g} s but the chair isn't moving: stuck or blocked"
# Holding a heading while driving straight: sideways stick per degree off, and the most of
# it. Measured at 10% forward: 5% sideways turns the chair about 4 degrees a second, and
# with none it veers right about 1 degree a second.
HEADING_PCT_PER_DEG = 1.0
HEADING_MAX_PCT = 8.0
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
        obstacle ahead or a chair that is not moving, and never drives longer than MAX_PATH_S.
        """
        path = np.asarray(path_xy, dtype=np.float64).reshape(-1, 2)
        along = np.cumsum(np.linalg.norm(np.diff(path, axis=0, prepend=[[0.0, 0.0]]), axis=1))
        length = float(along[-1])
        if not self._chair.gyro_fresh():
            return "failed", _drove(0.0, length, "the gyro is not reporting, so it did not start")
        stick = self._chair.setting("drive_stick_pct")
        ramp = max(0.0, self._chair.setting("ramp_s"))
        speed = self._chair.speed_for(stick)
        if speed <= 0:
            return "failed", _drove(0.0, length, "no speed is known for that stick")
        yaw, x, y, gone, v = self._chair.yaw_rad(), 0.0, 0.0, 0.0, 0.0
        started = last = self._clock()
        was, quiet = 0.0, None
        while (now := self._clock() - started) < MAX_PATH_S and length - gone > 0.02:
            if why := self._stopped():
                return "aborted", _drove(gone, length, why)
            if why := self._chair.blocked(v):
                self._chair.hold()
                self._chair.note("stopped for an obstacle", why=why)
                return "failed", _drove(gone, length, why)
            if not self._chair.gyro_fresh():
                self._chair.hold()
                return "failed", _drove(gone, length, "the gyro stopped reporting")
            heading = self._chair.yaw_rad() - yaw
            tick = self._clock() - last
            step, last = v * tick, self._clock()
            quiet = self._quiet(quiet, gone, v >= speed / 2, heading - was, tick)
            was = heading
            if quiet and last - quiet[0] >= STUCK_S:
                self._chair.hold()
                return "failed", _drove(quiet[1], length, STUCK)
            x, y, gone = x + step * math.cos(heading), y + step * math.sin(heading), gone + step
            # What is left of the path, as seen from the chair now.
            ahead, left = (path[np.searchsorted(along, min(gone, length)) :] - [x, y]).T
            rest = np.column_stack(
                [
                    ahead * math.cos(heading) + left * math.sin(heading),
                    -ahead * math.sin(heading) + left * math.cos(heading),
                ]
            )
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
                return "aborted", _drove(gone, length, problem)
            self._sleep(0.1)
        self._chair.hold()
        turned = math.degrees(self._chair.yaw_rad() - yaw)
        self._chair.note(
            "path", asked_m=round(length, 2), believed_m=round(gone, 2), turned_deg=round(turned, 1)
        )
        capped = (
            f"the {MAX_PATH_S:.0f} s allowed for one path ran out" if length - gone > 0.1 else ""
        )
        return "finished", _drove(gone, length, capped) + f"; gyro measured {_degrees(turned)}"

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
            return "failed", f"There is no tool called {call.name}."
        if not isinstance(amount, int | float) or not math.isfinite(amount) or amount <= 0:
            # A negative amount is not "the other way": the model names the direction by tool.
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
        stick = self._chair.setting("drive_stick_pct")
        ramp = max(0.0, self._chair.setting("ramp_s"))
        speed = self._chair.speed_for(stick)
        way = "forward" if forward else "back"
        if speed <= 0:
            return "failed", _drove(0.0, distance_m, "no speed is known for that stick", way)
        # Easing in and out each cover half the ground of the same time at full stick. The
        # believed speed is a guess (nothing measures distance), so a move never runs past
        # MAX_MOVE_S whatever it says.
        seconds, started = min(distance_m / speed + ramp, MAX_MOVE_S), self._clock()
        gone, y, last = 0.0, 0.0, started
        heading = was = self._chair.yaw_rad()
        quiet = None
        while (now := self._clock() - started) < seconds:
            tick = self._clock() - last
            gone += self._chair.speed_for(y) * tick
            last = self._clock()
            if why := self._stopped():
                return "aborted", _drove(gone, distance_m, why, way)
            if forward and (why := self._chair.blocked(self._chair.speed_for(y))):
                self._chair.hold()
                self._chair.note("stopped for an obstacle", why=why)
                return "failed", _drove(gone, distance_m, why, way)
            yaw = self._chair.yaw_rad()
            quiet = self._quiet(quiet, gone, y >= stick / 2, yaw - was, tick)
            was = yaw
            if quiet and last - quiet[0] >= STUCK_S:
                self._chair.hold()
                return "failed", _drove(quiet[1], distance_m, STUCK, way)
            # Once the chair is moving, a small sideways stick is a small, smooth turn
            # (hardware/wheelchair.py adds the turn dead zone only when standing), so the
            # heading can be held in proportion. Without the gyro the stick stays straight.
            off = math.degrees(heading - yaw) if self._chair.gyro_fresh() else 0.0
            x = -max(-HEADING_MAX_PCT, min(HEADING_MAX_PCT, HEADING_PCT_PER_DEG * off))
            eased = min(1.0, now / ramp, (seconds - now) / ramp) if ramp > 0 else 1.0
            y = max(1.0, stick * eased)
            if problem := self._chair.drive(x, y if forward else -y, reverse=not forward):
                return "aborted", _drove(gone, distance_m, problem, way)
            self._sleep(0.1)
        gone += self._chair.speed_for(y) * (self._clock() - last)
        self._chair.hold()
        self._chair.note(
            "straight" if forward else "back",
            asked_m=round(distance_m, 3),
            believed_m=round(gone, 3),
            seconds=round(seconds, 2),
            stick_pct=stick,
        )
        capped = (
            f"the {MAX_MOVE_S:.0f} s allowed for one move ran out"
            if distance_m - gone > 0.05
            else ""
        )
        return "finished", _drove(gone, distance_m, capped, way)

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
            return "disarmed"
        if not self._chair.page_alive():
            return "the bench page stopped watching (closed, or not answering)"
        return None

    def _phase(self, phase: str, **more: Any) -> None:
        self._state = {**self._state, "phase": phase, **more}

    def _turn(self, degrees_left: float) -> tuple[str, str]:
        """Rotate on the spot by an angle, by the gyro. Positive is to the left.

        The chair keeps turning for a moment after the stick lets go, so what is steered is
        where it would end up if let go now (its angle plus its turn rate times
        ``turn_coast_s``). The stick is in proportion to what is then left to turn: full
        ``turn_stick_pct`` beyond ``turn_slow_deg``, easing down to ``turn_min_pct``. It lets
        go once that is within ``turn_done_deg``, waits for the chair to stand still, and
        pushes again (either way) if it did not end there. A stick that is not turning the
        chair is raised little by little, and given up on after STUCK_S.

        Whatever ends it, what is said is what was asked and what the gyro measured.
        """
        asked = _degrees(degrees_left)
        if not self._chair.gyro_fresh():
            return "failed", f"asked {asked}, did not turn: the gyro is not reporting"
        start = self._chair.yaw_rad()
        goal = start + math.radians(degrees_left)
        cap_s = 6.0 + abs(degrees_left) * TURN_TIMEOUT_S_PER_DEG
        most = self._chair.setting("turn_stick_pct")
        least = min(most, self._chair.setting("turn_min_pct"))
        slow = max(1.0, self._chair.setting("turn_slow_deg"))
        close_enough = self._chair.setting("turn_done_deg")
        coast_s = self._chair.setting("turn_coast_s")
        if self._state.get("kind") == "turn":
            self._phase("turning")

        def measured(why: str = "") -> str:
            got = math.degrees(self._chair.yaw_rad() - start)
            return f"asked {asked}, gyro measured {_degrees(got)}{why}"

        began = self._clock()
        # (when, yaw) over the last RATE_WINDOW_S, for the turn rate.
        seen: deque[tuple[float, float]] = deque()
        # The push under way: when it began, the yaw then, and the stick added since.
        pushing: tuple[float, float] | None = None
        raised, still_since, top = 0.0, None, 0.0
        while True:
            now, yaw = self._clock(), self._chair.yaw_rad()
            if why := self._stopped():
                return "aborted", measured(f": {why}")
            if not self._chair.gyro_fresh():
                self._chair.hold()
                return "failed", measured(": the gyro stopped reporting, so the turn was stopped")
            if now - began > cap_s:
                self._chair.hold()
                return "failed", measured(f": the {cap_s:.0f} s allowed for this turn ran out")
            seen.append((now, yaw))
            while now - seen[0][0] > RATE_WINDOW_S:
                seen.popleft()
            rate = math.degrees(yaw - seen[0][1]) / (now - seen[0][0]) if now > seen[0][0] else 0.0
            to_go = math.degrees(goal - yaw) - rate * coast_s
            if abs(to_go) <= close_enough:
                if pushing:
                    self._chair.hold()
                pushing, raised = None, 0.0
                if abs(rate) >= STILL_DEG_PER_S:
                    still_since = None
                elif still_since is None:
                    still_since = now
                elif now - still_since >= STILL_S:
                    break
            else:
                still_since = None
                if pushing is None:
                    pushing = (now, yaw)
                elif now - pushing[0] >= STUCK_S:
                    if abs(math.degrees(yaw - pushing[1])) < STUCK_DEG:
                        self._chair.hold()
                        return "failed", measured(
                            f": {STUCK} (up to {top:.0f}% stick, and the gyro shows no rotation)"
                        )
                    pushing = (now, yaw)
                if abs(rate) < STILL_DEG_PER_S:
                    raised = min(most, raised + RAISE_PCT_PER_S * TURN_TICK_S)
                stick = min(most, max(least, most * abs(to_go) / slow) + raised)
                top = max(top, stick)
                # Stick right is positive; a left turn is stick left.
                if problem := self._chair.drive(-math.copysign(stick, to_go), 0.0):
                    return "aborted", measured(f": {problem}")
            self._sleep(TURN_TICK_S)
        got = math.degrees(self._chair.yaw_rad() - start)
        self._chair.note(
            "turn",
            asked_deg=degrees_left,
            got_deg=round(got, 1),
            seconds=round(self._clock() - began, 1),
            top_stick_pct=round(top, 1),
        )
        return "finished", measured()


def _drove(gone_m: float, asked_m: float, stopped: str = "", way: str = "") -> str:
    """How a straight or a path went: believed distance of what was asked, and why it ended."""
    said = f"drove {way + ' ' if way else ''}about {gone_m:.2f} m of {asked_m:.2f} m (believed)"
    return f"{said}, then stopped: {stopped}" if stopped else said


def _degrees(left: float) -> str:
    """An angle to the left in words: "left 25°"."""
    return f"{'left' if left >= 0 else 'right'} {abs(left):.0f}°"
