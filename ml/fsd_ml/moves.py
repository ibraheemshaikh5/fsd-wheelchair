"""Run the chair to a named thing on its own, one supervised episode at a time.

An episode is: find the target, then step toward it round whatever is in the way, then stop
on arriving. A turn is the other thing it can do: rotate on the spot by an angle, measured
by the phone's gyro. Between them they are enough to send the chair somewhere and bring it
back, so the same run can be repeated and compared.

    runner = Runner(chair)                 # ``chair`` is the viewer's session
    runner.go("go to the red pillow")      # returns at once; the work happens on a thread
    runner.state()                         # what it is doing, for the page and for whoever asked
    runner.abort("why")                    # stops the chair now

Nothing here moves the chair unless ``chair.armed()`` is true, and that is only ever set by
a person on the viewer's page. Every way an episode can end stops the chair first.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Looking for the target before moving at all.
FIND_S = 12.0
# Searching: turn this far, then stand still this long for the detector to look.
SEARCH_STEP_DEG = 25.0
SEARCH_LOOK_S = 4.0
SEARCH_LIMIT_DEG = 400.0
# Approaching: give up after this long with every way blocked, or with the target lost.
BLOCKED_S = 10.0
LOST_S = 3.0
# How the chair is driven (stick strengths, ramps, how early a turn lets go) comes from the
# bench's settings, read afresh on every move: see fsd_ml/bench.py.
TURN_TIMEOUT_S_PER_DEG = 0.12
# The turn rate full stick is believed to give; only a starting point, the gyro corrects it.


def _wrap(angle_rad: float) -> float:
    """An angle as the short way round: between a half turn left and a half turn right."""
    return math.atan2(math.sin(angle_rad), math.cos(angle_rad))


class Chair(Protocol):
    """What the runner needs from the viewer's session."""

    def armed(self) -> bool: ...
    def begin(self) -> str | None: ...  # start sending; a reason if it cannot
    def end(self) -> None: ...  # stop the chair and stop sending
    def step(self, say: str) -> dict[str, Any]: ...  # one decision, sent to the chair
    def hold(self) -> None: ...  # stand still for now, staying ready
    def turn(self, stick_pct: float) -> str | None: ...  # stick right is positive
    def yaw_rad(self) -> float: ...  # how far the chair has turned, positive left
    def drive(
        self, x_pct: float, y_pct: float, reverse: bool = False
    ) -> str | None: ...  # raw stick
    def observe(self) -> tuple[bytes | None, str]: ...  # the camera's picture and a short report
    def speed_for(self, y_pct: float) -> float: ...  # the speed that stick is believed to give
    def blocked(self, v_mps: float, w_radps: float) -> str | None: ...  # what is in the way
    def note(self, event: str, **fields: Any) -> None: ...  # a line for the run's record
    def setting(self, name: str) -> float: ...  # how the chair is driven; read afresh each time
    def pose(self) -> tuple[float, float, float]: ...  # metres ahead and left of the start, yaw
    def finished(self, record: dict[str, Any]) -> None: ...  # the run is over; here is how it went
    def target(self) -> dict[str, Any]: ...  # {"locked": bool, "note": str | None}


@dataclass(frozen=True, slots=True)
class Limits:
    max_steps: int = 80
    max_seconds: float = 120.0
    search: bool = True
    search_left: bool = True


class Runner:
    def __init__(
        self,
        chair: Chair,
        record_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._chair, self._record_dir = chair, record_dir
        self._clock, self._sleep = clock, sleep
        self._thread: threading.Thread | None = None
        self._abort: str | None = None
        self._count = 0
        self._state: dict[str, Any] = {"running": False, "phase": "idle"}
        self.history: list[dict[str, Any]] = []

    def running(self) -> bool:
        return bool(self._state.get("running"))

    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def abort(self, why: str) -> None:
        """Stop whatever is running. Safe to call at any time, from any thread."""
        if self.running():
            self._abort = why

    def go(self, say: str, limits: Limits | None = None, background: bool = True) -> dict[str, Any]:
        return self._start("go", lambda: self._go(say, limits or Limits()), background, say=say)

    def turn(self, degrees_left: float, background: bool = True) -> dict[str, Any]:
        return self._start(
            "turn", lambda: self._turn(degrees_left), background, degrees_left=degrees_left
        )

    def straight(self, distance_m: float, background: bool = True) -> dict[str, Any]:
        """Drive straight for a distance. The real distance, measured, calibrates the speed."""

        def work() -> tuple[str, str]:
            result = self._straight(distance_m)
            return ("finished", result[1]) if result[0] == "drove" else result

        return self._start("straight", work, background, distance_m=distance_m)

    def pilot(
        self, say: str, pilot: Any, max_turns: int = 30, background: bool = True
    ) -> dict[str, Any]:
        """Let a model drive: show it the picture, carry out the moves it asks for, repeat.

        ``pilot`` is a fsd_ml.pilot.Pilot (or anything with its ``next`` and ``last``). The
        model chooses the moves; this runner carries them out and stops them when it must.
        """
        return self._start(
            "pilot", lambda: self._pilot(pilot, max_turns), background, say=say, pilot=True
        )

    def _pilot(self, pilot: Any, max_turns: int) -> tuple[str, str]:
        results: list[tuple[str, str, bool]] | None = None
        for turn in range(1, max_turns + 1):
            if why := self._stopped():
                return "aborted", why
            self._settle(0.4)  # stand still, so the picture is sharp and shows where it ended
            jpeg, report = self._chair.observe()
            waited = 0.0
            while jpeg is None and waited < 5.0 and not self._stopped():
                self._sleep(0.25)
                waited += 0.25
                jpeg, report = self._chair.observe()
            if jpeg is None:
                return "failed", "no camera picture to show the pilot"
            self._phase(f"look {turn}: the pilot is thinking", steps=turn)
            calls = pilot.next(jpeg, report, results)
            self._chair.note(
                "pilot",
                turn=turn,
                report=report,
                calls=[{"name": c.name, **c.input} for c in calls],
                **pilot.last,
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
                self._phase(f"look {turn}: {call.says()} · {call.why}", steps=turn)
                outcome, text = self._carry_out(call)
                if outcome == "aborted":
                    return outcome, text
                cut_short = outcome == "failed"
                results.append((call.id, text, cut_short))
        return "failed", f"not done after {max_turns} looks"

    def _carry_out(self, call: Any) -> tuple[str, str]:
        """One move the pilot asked for, cut to what is allowed.

        Returns "ok", "failed" or "aborted", and what to tell the pilot about it.
        """
        from fsd_ml.pilot import MAX_BACKWARD_M, MAX_FORWARD_M, MAX_TURN_DEG

        if call.name in ("move_forward", "move_backward"):
            forward = call.name == "move_forward"
            asked = abs(float(call.input.get("distance_m", 0.0)))
            distance = max(0.05, min(MAX_FORWARD_M if forward else MAX_BACKWARD_M, asked))
            outcome, text = self._straight(distance) if forward else self._back(distance)
            if outcome == "drove":
                cut = (
                    f" (you asked for {asked:.2f} m; that is the most allowed)"
                    if asked > distance
                    else ""
                )
                way = "forward" if forward else "back"
                return "ok", f"Drove {way} about {distance:.2f} m{cut}."
            return ("aborted", text) if outcome == "aborted" else ("failed", f"Stopped: {text}")
        if call.name in ("turn_left", "turn_right"):
            asked = abs(float(call.input.get("degrees", 0.0)))
            degrees = max(3.0, min(MAX_TURN_DEG, asked))
            outcome, text = self._turn(degrees if call.name == "turn_left" else -degrees)
            if outcome == "turned":
                return "ok", f"The gyro says the chair {text}."
            return ("aborted", text) if outcome == "aborted" else ("failed", f"Stopped: {text}")
        return "failed", f"There is no tool called {call.name}."

    def _back(self, distance_m: float) -> tuple[str, str]:
        """Drive straight back a short way, eased in and out. Nothing sees behind the chair."""
        stick = self._chair.setting("drive_stick_pct")
        ramp = max(0.0, self._chair.setting("ramp_s"))
        speed = self._chair.speed_for(stick)
        if speed <= 0:
            return "failed", "no speed is known for that stick"
        seconds, started = distance_m / speed + ramp, self._clock()
        while (now := self._clock() - started) < seconds:
            if why := self._stopped():
                return "aborted", why
            eased = min(1.0, now / ramp, (seconds - now) / ramp) if ramp > 0 else 1.0
            if problem := self._chair.drive(0.0, -max(1.0, stick * eased), True):
                return "aborted", problem
            self._sleep(0.1)
        self._chair.hold()
        self._chair.note("back", asked_m=round(distance_m, 3), seconds=round(seconds, 2))
        return "drove", f"backed for {seconds:.1f} s"

    def home(self, background: bool = True) -> dict[str, Any]:
        """Go back to where "start" was set: face it, drive to it, turn to the start's heading.

        It goes by how far the chair believes it has moved, so it is as good as the speed
        calibration and no better; near enough to save carrying the chair back.
        """

        def work() -> tuple[str, str]:
            ahead, left, yaw = self._chair.pose()
            away = math.hypot(ahead, left)
            if away > 0.15:
                self._phase("facing the start")
                facing = math.atan2(-left, -ahead)
                result = self._turn(math.degrees(_wrap(facing - yaw)))
                if result[0] != "turned":
                    return result
                self._settle()
                self._phase("driving to the start")
                result = self._straight(away)
                if result[0] != "drove":
                    return result
                self._settle()
            self._phase("turning to the start's heading")
            result = self._turn(math.degrees(_wrap(-self._chair.pose()[2])))
            if result[0] != "turned":
                return result
            return "finished", f"returned from {away:.2f} m away"

        return self._start("home", work, background)

    def circle(
        self, diameter_m: float, left: bool = True, background: bool = True
    ) -> dict[str, Any]:
        """Drive one full circle. Its real width, measured on the floor, calibrates the speed."""
        return self._start(
            "circle",
            lambda: self._arc(math.tau if left else -math.tau, diameter_m / 2),
            background,
            diameter_m=diameter_m,
            left=left,
        )

    def square(self, side_m: float, left: bool = True, background: bool = True) -> dict[str, Any]:
        """Drive a square: four straight sides with a quarter turn on the spot after each."""

        def work() -> tuple[str, str]:
            for side in range(4):
                self._phase(f"side {side + 1} of 4")
                result = self._straight(side_m)
                if result[0] != "drove":
                    return result
                self._settle()
                self._phase(f"corner {side + 1} of 4")
                result = self._turn(90.0 if left else -90.0)
                if result[0] != "turned":
                    return result
                self._settle()
            return "finished", f"drove a {side_m:.2f} m square"

        return self._start("square", work, background, side_m=side_m, left=left)

    def _settle(self, seconds: float | None = None) -> None:
        """Stand still for a moment so one move does not run into the next."""
        self._chair.hold()
        until = self._clock() + (self._chair.setting("settle_s") if seconds is None else seconds)
        while self._clock() < until and not self._stopped():
            self._sleep(0.1)

    def _straight(self, distance_m: float) -> tuple[str, str]:
        """Drive straight for a distance, easing up to speed and easing back down.

        Nothing measures how far the chair goes: it drives for the time the believed speed
        says. Measuring the real distance afterwards is how that belief is corrected.
        """
        stick = self._chair.setting("drive_stick_pct")
        ramp = max(0.0, self._chair.setting("ramp_s"))
        speed = self._chair.speed_for(stick)
        if speed <= 0:
            return "failed", "no speed is known for that stick"
        # Easing in and out each cover half the ground of the same time at full stick.
        seconds = distance_m / speed + ramp
        heading, started = self._chair.yaw_rad(), self._clock()
        while (now := self._clock() - started) < seconds:
            if why := self._stopped():
                return "aborted", why
            if why := self._chair.blocked(speed, 0.0):
                self._chair.hold()
                self._chair.note("straight stopped", why=why)
                return "failed", f"stopped for an obstacle: {why}"
            # Stick straight ahead and nothing else. Pushed straight, the chair goes straight;
            # and because of the stick's dead zone the smallest sideways nudge is a real turn,
            # so "correcting" the heading here only makes it weave.
            eased = min(1.0, now / ramp, (seconds - now) / ramp) if ramp > 0 else 1.0
            if problem := self._chair.drive(0.0, max(1.0, stick * eased)):
                return "aborted", problem
            self._sleep(0.1)
        self._chair.hold()
        drift = math.degrees(self._chair.yaw_rad() - heading)
        self._chair.note(
            "straight",
            asked_m=round(distance_m, 3),
            seconds=round(seconds, 2),
            stick_pct=stick,
            ramp_s=ramp,
            drift_deg=round(drift, 1),
        )
        return "drove", f"drove for {seconds:.1f} s at {speed:.2f} m/s believed"

    def _arc(self, angle_rad: float, radius_m: float) -> tuple[str, str]:
        """Drive round an arc: forward at a fixed stick, turning at the rate that radius needs.

        The turn rate is held by the gyro, so the angle is right; the radius is right only if
        the believed speed is, which is what measuring the circle on the floor tells us.
        """
        stick_pct = self._chair.setting("drive_stick_pct")
        speed = self._chair.speed_for(stick_pct)
        if speed <= 0 or radius_m <= 0:
            return "failed", "no speed is known for that stick"
        rate = math.copysign(speed / radius_m, angle_rad)  # rad/s, positive left
        seconds = abs(angle_rad / rate)
        start, started = self._chair.yaw_rad(), self._clock()
        self._phase("driving the arc", turn_rate_dps=round(math.degrees(rate), 1))
        while True:
            if why := self._stopped():
                return "aborted", why
            now = self._clock() - started
            turned = self._chair.yaw_rad() - start
            if abs(turned) >= abs(angle_rad):
                self._chair.hold()
                return "finished", (
                    f"turned {math.degrees(turned):+.0f} degrees in {now:.1f} s"
                    f" (planned {seconds:.1f} s)"
                )
            if now > seconds * 2.5 + 3.0:
                self._chair.hold()
                return "failed", f"only turned {math.degrees(turned):+.0f} degrees in {now:.0f} s"
            if why := self._chair.blocked(speed, rate):
                self._chair.hold()
                return "failed", f"stopped for an obstacle: {why}"
            # Where the heading should be by now, against where it is; stick right is positive.
            behind_deg = math.degrees(rate * min(now, seconds) - turned)
            feed = 100.0 * rate / self._chair.setting("w_full_radps")
            stick = -(feed + 0.6 * behind_deg)
            if problem := self._chair.drive(max(-30.0, min(30.0, stick)), stick_pct):
                return "aborted", problem
            self._phase("driving the arc", turned_deg=round(math.degrees(turned)))
            self._sleep(0.1)

    def _start(
        self, kind: str, work: Callable[[], tuple[str, str]], background: bool, **asked: Any
    ) -> dict[str, Any]:
        if self.running():
            raise RuntimeError("something is already running")
        if not self._chair.armed():
            raise RuntimeError("not armed: tick “Let Claude drive” on the viewer page first")
        self._count += 1
        self._abort = None
        self._state = {
            "running": True,
            "id": self._count,
            "kind": kind,
            **asked,
            "phase": "starting",
            "steps": 0,
            "started": time.time(),
        }

        def run() -> None:
            started = self._clock()
            statuses: Counter[str] = Counter()
            self._statuses = statuses
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
                "outcome": outcome,
                "reason": reason,
                "seconds": round(self._clock() - started, 1),
                "statuses": dict(statuses),
            }
            self.history.append(self._state)
            self._save(self._state)
            self._chair.finished(self._state)

        if background:
            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
        else:
            run()
        return self.state()

    def _save(self, record: dict[str, Any]) -> None:
        if self._record_dir is None:
            return
        try:
            self._record_dir.mkdir(parents=True, exist_ok=True)
            name = f"{time.strftime('%Y%m%d-%H%M%S')}-{record['id']:03d}-{record['kind']}.json"
            (self._record_dir / name).write_text(json.dumps(record, indent=2) + "\n")
        except (OSError, TypeError, ValueError):
            pass

    def _stopped(self) -> str | None:
        """Why the run must end now, if it must."""
        if self._abort:
            return self._abort
        if not self._chair.armed():
            return "disarmed"
        return None

    def _phase(self, phase: str, **more: Any) -> None:
        self._state = {**self._state, "phase": phase, **more}

    def _wait_for_lock(self, seconds: float) -> bool | None:
        """True once the target is locked; False if not in time; None if the run must end."""
        until = self._clock() + seconds
        while self._clock() < until:
            if self._stopped():
                return None
            self._chair.hold()
            seen = self._chair.target()
            self._phase(self._state["phase"], note=seen.get("note"))
            if seen.get("locked"):
                return True
            self._sleep(0.2)
        return False

    def _go(self, say: str, limits: Limits) -> tuple[str, str]:
        started = self._clock()
        self._phase("finding the target")
        found = self._wait_for_lock(FIND_S)
        if found is None:
            return "aborted", self._stopped() or "stopped"
        if not found:
            if not limits.search:
                return "failed", "target not found from the start position"
            result = self._search(limits.search_left)
            if result is not None:
                return result

        blocked_since = lost_since = None
        while True:
            if why := self._stopped():
                return "aborted", why
            if self._state["steps"] >= limits.max_steps:
                return "failed", f"not there after {limits.max_steps} steps"
            if self._clock() - started > limits.max_seconds:
                return "failed", f"not there after {limits.max_seconds:.0f} s"
            answer = self._chair.step(say)
            now = self._clock()
            status = answer.get("waiting") and "waiting" or answer.get("status") or "unknown"
            self._statuses[status] += 1
            chair = answer.get("chair") or {}
            self._phase(
                "approaching",
                steps=self._state["steps"] + 1,
                status=status,
                reason=answer.get("reason") or answer.get("waiting"),
                target_xy=answer.get("target_xy"),
                steer=answer.get("steer"),
            )
            if chair.get("halted"):
                return "aborted", chair.get("problem") or "the chair took control back"
            if status == "arrived":
                return "arrived", f"stopped {answer.get('target_gap_m', '?')} m from the target"
            if answer.get("steer") == "target":
                lost_since = None
            else:
                lost_since = lost_since or now
                if now - lost_since > LOST_S:
                    self._chair.hold()
                    if not limits.search:
                        return "failed", "lost the target"
                    result = self._search(limits.search_left)
                    if result is not None:
                        return result
                    lost_since = None
                    continue
            if status in ("blocked", "waiting"):
                blocked_since = blocked_since or now
                if now - blocked_since > BLOCKED_S:
                    return "failed", f"stuck: {answer.get('reason') or answer.get('waiting')}"
            else:
                blocked_since = None

    def _search(self, left: bool) -> tuple[str, str] | None:
        """Turn on the spot, a step at a time, until the detector locks. None means found."""
        turned = 0.0
        while turned < SEARCH_LIMIT_DEG:
            self._phase("searching", turned_deg=round(turned))
            result = self._turn(SEARCH_STEP_DEG if left else -SEARCH_STEP_DEG)
            if result[0] != "turned":
                return result
            turned += SEARCH_STEP_DEG
            self._phase("searching", turned_deg=round(turned))
            found = self._wait_for_lock(SEARCH_LOOK_S)
            if found is None:
                return "aborted", self._stopped() or "stopped"
            if found:
                return None
        return "failed", "turned all the way round without finding the target"

    def _turn(self, degrees_left: float) -> tuple[str, str]:
        """Rotate on the spot by an angle, by the gyro. Positive is to the left.

        The stick is released early, because the chair coasts; then it stands still, the
        gyro says where it really ended up, and it is nudged again if that is still off.
        """
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
                if self._clock() > deadline:
                    self._chair.hold()
                    done = math.degrees(self._chair.yaw_rad() - start)
                    return "failed", (
                        f"turned only {done:+.0f} of {degrees_left:+.0f} degrees:"
                        " the chair is not turning, or the gyro is not reporting"
                    )
                if problem := self._chair.turn(stick):
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
        return "turned", f"turned {done:+.0f} degrees (asked {degrees_left:+.0f})"
