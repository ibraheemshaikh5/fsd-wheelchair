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
"""

from __future__ import annotations

import math
import threading
import time
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


# Longest a straight or backing move may drive, however slow the chair is believed to be.
MAX_MOVE_S = 4.0
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
                return "failed", "no fresh camera picture to show Gemini"
            paths, tilt = self._chair.candidates()
            picture = candidates.draw(frame.jpeg, paths, self._chair.setup, tilt)
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
            ended = "done" if result == "finished" else "cut short"
            outcome = f"{choice} ({words}), {ended}: {text}."
        return "failed", f"not done after {max_looks} looks"

    def _follow(self, path_xy: np.ndarray) -> tuple[str, str]:
        """Drive along a path laid on the floor from where the chair stands now.

        The heading is measured (the gyro); how far it has gone is believed (speed x time, as
        in a straight). Each tick the rest of the path is seen from where the chair is thought
        to be, and the path follower (pipeline/control.py) steers for it. It stops for an
        obstacle ahead, and never drives longer than MAX_PATH_S.
        """
        if not self._chair.gyro_fresh():
            return "failed", "the gyro is not reporting, so a path cannot be followed"
        stick = self._chair.setting("drive_stick_pct")
        ramp = max(0.0, self._chair.setting("ramp_s"))
        speed = self._chair.speed_for(stick)
        if speed <= 0:
            return "failed", "no speed is known for that stick"
        path = np.asarray(path_xy, dtype=np.float64).reshape(-1, 2)
        along = np.cumsum(np.linalg.norm(np.diff(path, axis=0, prepend=[[0.0, 0.0]]), axis=1))
        length = float(along[-1])
        seconds = min(length / speed + ramp, MAX_PATH_S)
        yaw, x, y, gone, v = self._chair.yaw_rad(), 0.0, 0.0, 0.0, 0.0
        started = last = self._clock()
        while (now := self._clock() - started) < seconds:
            if why := self._stopped():
                return "aborted", why
            if why := self._chair.blocked():
                self._chair.hold()
                self._chair.note("stopped for an obstacle", why=why)
                return "failed", f"stopped for an obstacle after about {gone:.1f} m: {why}"
            if not self._chair.gyro_fresh():
                self._chair.hold()
                return "failed", f"the gyro stopped reporting after about {gone:.1f} m"
            heading = self._chair.yaw_rad() - yaw
            step = v * (self._clock() - last)
            last = self._clock()
            x, y, gone = x + step * math.cos(heading), y + step * math.sin(heading), gone + step
            # What is left of the path, as seen from the chair now.
            ahead, left = (path[np.searchsorted(along, min(gone, length)) :] - [x, y]).T
            rest = np.column_stack(
                [
                    ahead * math.cos(heading) + left * math.sin(heading),
                    -ahead * math.sin(heading) + left * math.cos(heading),
                ]
            )
            eased = min(1.0, now / ramp, (seconds - now) / ramp) if ramp > 0 else 1.0
            v, w = follow(rest, max(eased, 1.0 / stick), speed_limit_mps=speed)
            if length - gone < 0.3:
                w = 0.0  # too near the end to aim at it: the aim swings wide as it closes
            turn, forward = to_stick(
                v, w, self._chair.setting("v_full_mps"), self._chair.setting("w_full_radps")
            )
            # Never a harder turn than a turn on the spot is given.
            most = self._chair.setting("turn_stick_pct")
            if problem := self._chair.drive(max(-most, min(most, turn)), forward):
                return "aborted", problem
            self._sleep(0.1)
        self._chair.hold()
        self._chair.note("path", asked_m=round(length, 2), believed_m=round(gone, 2))
        return "finished", f"drove about {gone:.1f} m of {length:.1f} m along it"

    def _look(self) -> tuple[Any, str]:
        """Stand still, then a fresh camera frame and the obstacle report; no frame if none came."""
        self._settle(0.4)  # so the picture is sharp and shows where the chair ended up
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
                return "failed", "no fresh camera picture to show the pilot"
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
            distance = max(0.05, min(MAX_FORWARD_M if forward else MAX_BACKWARD_M, amount))
            outcome, text = self._straight(distance) if forward else self._back(distance)
            if outcome == "finished":
                cut = (
                    f" (you asked for {amount:.2f} m; that is the most allowed)"
                    if amount > distance
                    else ""
                )
                return (
                    "ok",
                    f"Drove {'forward' if forward else 'back'} about {distance:.2f} m{cut}.",
                )
        else:
            degrees = max(3.0, min(MAX_TURN_DEG, amount))
            outcome, text = self._turn(degrees if call.name == "turn_left" else -degrees)
            if outcome == "finished":
                return "ok", f"The gyro says the chair {text}."
        return ("aborted", text) if outcome == "aborted" else ("failed", f"Stopped: {text}")

    def _settle(self, seconds: float | None = None) -> None:
        """Stand still for a moment so one move does not run into the next."""
        self._chair.hold()
        until = self._clock() + (self._chair.setting("settle_s") if seconds is None else seconds)
        while self._clock() < until and not self._stopped():
            self._sleep(0.1)

    def _drive_for(self, distance_m: float, forward: bool) -> tuple[str, str]:
        """Hold the stick straight for the time a distance takes, eased in and out.

        Nothing measures how far the chair goes: it drives for the time the believed speed
        says. Going forward it stops for whatever the obstacle sense finds ahead; nothing
        sees behind the chair, so backing is kept short by its caller.
        """
        stick = self._chair.setting("drive_stick_pct")
        ramp = max(0.0, self._chair.setting("ramp_s"))
        speed = self._chair.speed_for(stick)
        if speed <= 0:
            return "failed", "no speed is known for that stick"
        # Easing in and out each cover half the ground of the same time at full stick. The
        # believed speed is a guess (nothing measures distance), so a move never runs past
        # MAX_MOVE_S whatever it says.
        seconds, started = min(distance_m / speed + ramp, MAX_MOVE_S), self._clock()
        while (now := self._clock() - started) < seconds:
            if why := self._stopped():
                return "aborted", why
            if forward and (why := self._chair.blocked()):
                self._chair.hold()
                self._chair.note("stopped for an obstacle", why=why)
                return "failed", f"stopped for an obstacle: {why}"
            # Stick straight and nothing else. Pushed straight, the chair goes straight; and
            # because of the stick's dead zone the smallest sideways nudge is a real turn, so
            # "correcting" the heading here only makes it weave.
            eased = min(1.0, now / ramp, (seconds - now) / ramp) if ramp > 0 else 1.0
            y = max(1.0, stick * eased)
            if problem := self._chair.drive(0.0, y if forward else -y, reverse=not forward):
                return "aborted", problem
            self._sleep(0.1)
        self._chair.hold()
        self._chair.note(
            "straight" if forward else "back",
            asked_m=round(distance_m, 3),
            seconds=round(seconds, 2),
            stick_pct=stick,
        )
        return "finished", f"drove for {seconds:.1f} s at {speed:.2f} m/s believed"

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

        The stick is released early, because the chair coasts; then it stands still, the
        gyro says where it really ended up, and it is nudged again if that is still off.
        Without a fresh gyro reading the turn cannot be measured, so it stops at once.
        """
        if not self._chair.gyro_fresh():
            return "failed", "the gyro is not reporting, so a turn cannot be measured"
        start = self._chair.yaw_rad()
        goal = start + math.radians(degrees_left)
        deadline = self._clock() + 6.0 + abs(degrees_left) * TURN_TIMEOUT_S_PER_DEG
        lead = min(self._chair.setting("turn_lead_deg"), abs(degrees_left) / 2)
        turn_stick = self._chair.setting("turn_stick_pct")
        close_enough = self._chair.setting("turn_done_deg")
        if self._state.get("kind") == "turn":
            self._phase("turning")
        passes = []
        for _ in range(max(1, round(self._chair.setting("turn_passes")))):
            to_go = math.degrees(goal - self._chair.yaw_rad())
            if abs(to_go) <= close_enough:
                break
            way = math.copysign(1.0, to_go)
            stick = -way * turn_stick  # stick right is positive; a left turn is stick left
            before = self._chair.yaw_rad()
            while way * math.degrees(goal - self._chair.yaw_rad()) > lead:
                if why := self._stopped():
                    return "aborted", why
                done = math.degrees(self._chair.yaw_rad() - start)
                if not self._chair.gyro_fresh():
                    self._chair.hold()
                    return "failed", f"the gyro stopped reporting after {done:+.0f} degrees"
                if self._clock() > deadline:
                    self._chair.hold()
                    return "failed", (
                        f"turned only {done:+.0f} of {degrees_left:+.0f} degrees:"
                        " the chair is not turning"
                    )
                if problem := self._chair.drive(stick, 0.0):
                    return "aborted", problem
                self._sleep(0.05)
            released = self._chair.yaw_rad()
            self._settle()
            if why := self._stopped():
                return "aborted", why
            coasted = math.degrees(self._chair.yaw_rad() - released)
            passes.append(
                {
                    "driven_deg": round(math.degrees(released - before), 1),
                    "coasted_deg": round(coasted, 1),
                }
            )
            # Next time, let go by as much as it just coasted.
            lead = max(2.0, min(40.0, abs(coasted) * 0.9))
        done = math.degrees(self._chair.yaw_rad() - start)
        self._chair.note(
            "turn",
            asked_deg=degrees_left,
            got_deg=round(done, 1),
            stick_pct=turn_stick,
            passes=passes,
        )
        return "finished", f"turned {done:+.0f} degrees (asked {degrees_left:+.0f})"
