"""One pass of the pipeline, and the trip state it runs under.

Stage numbers refer to docs/driving-pipeline.md. Stage 6 (obstacles) is not
here: obstacles arrive already detected in ``Sensors``.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from fsd_ml.drivers import Candidate, Driver, Goal, Motion
from fsd_ml.geo import LatLon, chair_to_world, to_local, world_to_chair
from fsd_ml.pipeline.control import ControlConfig, follow
from fsd_ml.pipeline.footprint import Choice, FootprintConfig, choose
from fsd_ml.pipeline.route import Route
from fsd_ml.pipeline.types import (
    ChairStatus,
    Decision,
    DriveCommand,
    Mode,
    Pose,
    Sensors,
)


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    # Freshness limits match the driving service's.
    frame_max_age_ms: float = 500.0
    fix_max_age_ms: float = 10_000.0
    # The oldest model result the chair will act on, counted from the frame it used.
    model_max_age_ms: float = 1_000.0
    # The model is aimed at the route point this far ahead.
    goal_lookahead_m: float = 6.0
    arrive_radius_m: float = 1.5
    # A stop lasting this long asks the rider to take over.
    take_over_after_gate_ms: float = 2_000.0
    take_over_after_blocked_ms: float = 10_000.0
    command_ttl_ms: float = 200.0
    # Where the lens sits relative to the middle of the chair, metres (ahead, left).
    # The model's paths start at the lens; they are shifted by this to start at the chair.
    lens_xy: tuple[float, float] = (0.0, 0.0)
    footprint: FootprintConfig = field(default_factory=FootprintConfig)
    control: ControlConfig = field(default_factory=ControlConfig)


@dataclass(frozen=True, slots=True)
class Proposal:
    candidates: tuple[Candidate, ...]
    # Where the chair was when the frames were taken; the paths are relative to it.
    pose: Pose | None
    frame_t_ms: float
    inference_ms: float


class ModelRunner:
    """Calls the driver at its own pace and hands back its newest finished result.

    ``latency_ms`` delays each result, standing in for inference and network
    time. A callable gets the request time, so a test can make the model stall.
    ``count_inference`` adds the time the call really took, so a slow or remote
    model is as late in the simulator as it would be live.
    """

    def __init__(
        self,
        driver: Driver,
        period_ms: float = 100.0,
        latency_ms: float | Callable[[float], float] = 0.0,
        count_inference: bool = False,
    ) -> None:
        self.driver = driver
        self._count_inference = count_inference
        self._period_ms = period_ms
        self._latency_ms = latency_ms
        self._latest: Proposal | None = None
        self._pending: tuple[float, Proposal] | None = None
        self._last_request_ms: float | None = None

    def update(
        self,
        now_ms: float,
        frames: list[np.ndarray],
        frame_t_ms: float,
        goal: Goal,
        motion: Motion,
        pose: Pose | None,
    ) -> Proposal | None:
        if self._pending is not None and now_ms >= self._pending[0]:
            self._latest = self._pending[1]
            self._pending = None

        due = self._last_request_ms is None or now_ms - self._last_request_ms >= self._period_ms
        if self._pending is None and due:
            started = time.perf_counter()
            candidates = self.driver.propose(frames, goal, motion)
            inference_ms = (time.perf_counter() - started) * 1000.0
            proposal = Proposal(tuple(candidates), pose, frame_t_ms, inference_ms)
            latency = self._latency_ms(now_ms) if callable(self._latency_ms) else self._latency_ms
            if self._count_inference:
                latency += inference_ms
            self._last_request_ms = now_ms
            if latency <= 0:
                self._latest = proposal
            else:
                self._pending = (now_ms + latency, proposal)
        return self._latest


def _in_current_frame(proposal: Proposal, pose: Pose | None) -> list[Candidate]:
    """The proposed paths as seen from where the chair is now, not where it was."""
    if proposal.pose is None or pose is None:
        return list(proposal.candidates)
    then = proposal.pose
    return [
        Candidate(
            world_to_chair(
                chair_to_world(candidate.path_xy, then.east, then.north, then.heading_deg),
                pose.east,
                pose.north,
                pose.heading_deg,
            ),
            candidate.score,
            candidate.speed_mps,
        )
        for candidate in proposal.candidates
    ]


class Pipeline:
    def __init__(
        self,
        driver: Driver,
        config: PipelineConfig | None = None,
        runner: ModelRunner | None = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.driver = driver
        self._runner = runner or ModelRunner(driver)
        self.mode: Mode = "idle"
        self.route: Route | None = None
        self.instruction: str | None = None
        self.origin: LatLon | None = None
        self.progress_m = 0.0
        self._seq = 0
        self._frames: deque[np.ndarray] = deque(maxlen=driver.context_frames)
        # Newest frame received, and newest frame kept for the model.
        self._frame_t_ms: float | None = None
        self._kept_t_ms: float | None = None
        self._last_speed = 0.0
        self._gate_since_ms: float | None = None
        self._blocked_since_ms: float | None = None

    # Commands from the iPad.

    def set_route(self, route: Route) -> None:
        """An outdoor trip along a planned route. The chair holds until engaged."""
        self.route, self.instruction = route, None
        self.origin = route.origin
        self.progress_m = 0.0
        self.mode = "ready"

    def set_instruction(self, instruction: str) -> None:
        """An indoor trip from a spoken instruction. There is no route or progress."""
        self.route, self.instruction = None, instruction
        self.progress_m = 0.0
        self.mode = "ready"

    def clear(self) -> None:
        self.route = self.instruction = None
        self.progress_m = 0.0
        self.mode = "idle"

    def engage(self) -> None:
        if self.mode in ("ready", "arrived") and self._has_trip():
            self.mode = "driving"
            self._gate_since_ms = self._blocked_since_ms = None

    def disengage(self) -> None:
        if self.mode == "driving":
            self.mode = "ready"

    def _has_trip(self) -> bool:
        return self.route is not None or self.instruction is not None

    # One pass.

    def step(self, sensors: Sensors, chair: ChairStatus, dt_s: float) -> Decision:
        config = self.config
        now = sensors.t_ms

        # Stage 4: frames. Late or repeated frames are dropped, and the model's
        # buffer is thinned to the frame rate the model expects.
        if (
            sensors.frame is not None
            and sensors.frame_t_ms is not None
            and (self._frame_t_ms is None or sensors.frame_t_ms > self._frame_t_ms)
        ):
            self._frame_t_ms = sensors.frame_t_ms
            gap_ms = 1000.0 / self.driver.frame_hz
            if self._kept_t_ms is None or sensors.frame_t_ms - self._kept_t_ms >= gap_ms - 1.0:
                self._frames.append(sensors.frame)
                self._kept_t_ms = sensors.frame_t_ms

        pose = self._localize(sensors)
        self._follow_chair(chair)
        goal, remaining = self._goal(pose)

        # Stage 5: model.
        proposal = None
        if (
            goal is not None
            and self._kept_t_ms is not None
            and len(self._frames) == (self._frames.maxlen)
        ):
            motion = Motion(sensors.speed_mps, sensors.yaw_rate_radps)
            proposal = self._runner.update(
                now, list(self._frames), self._kept_t_ms, goal, motion, pose
            )

        # Stage 7: choose a path the chair fits along.
        candidates = _in_current_frame(proposal, pose) if proposal else []
        if config.lens_xy != (0.0, 0.0):
            candidates = [
                Candidate(c.path_xy + config.lens_xy, c.score, c.speed_mps) for c in candidates
            ]
        if candidates:
            choice = choose(candidates, sensors.obstacles, config.footprint)
        else:
            choice = Choice(None, np.empty((0, 2)), "clear", 0.0, None, None)

        gate_reason = self._gate(sensors, chair, proposal, now)
        speed = sensors.speed_mps if sensors.speed_mps is not None else self._last_speed

        v = w = 0.0
        reason = choice.reason
        take_over = None
        if self.mode == "driving":
            if gate_reason is not None:
                if self._gate_since_ms is None:
                    self._gate_since_ms = now
                reason = gate_reason
                waited = now - self._gate_since_ms
                if chair.estop or waited >= config.take_over_after_gate_ms:
                    take_over = gate_reason
            else:
                self._gate_since_ms = None
                if choice.status == "blocked":
                    if self._blocked_since_ms is None:
                        self._blocked_since_ms = now
                    if now - self._blocked_since_ms >= config.take_over_after_blocked_ms:
                        take_over = "Path blocked"
                else:
                    self._blocked_since_ms = None
                    # Stage 8: control.
                    # Steer by the path as the model drew it; the lens shift is for the
                    # fit check only, or the chair would drift toward the camera's side.
                    v, w = follow(
                        choice.path_xy - config.lens_xy,
                        choice.speed_factor,
                        speed,
                        dt_s,
                        config.control,
                        choice.speed_limit_mps,
                    )
        elif self.mode == "arrived":
            reason = "Arrived"
        self._last_speed = v

        self._seq += 1
        return Decision(
            command=DriveCommand(self._seq, now, v, w, config.command_ttl_ms),
            mode=self.mode,
            path_status=choice.status,
            path_xy=choice.path_xy,
            obstacles=sensors.obstacles,
            yielding_to_id=choice.yielding_to_id,
            reason=reason,
            take_over=take_over,
            engage_blocked=self._engage_blocked(chair, gate_reason),
            pose=pose,
            progress_m=self.progress_m,
            remaining_m=remaining,
            gate_reason=gate_reason,
            model_name=self.driver.name,
            model_age_ms=None if proposal is None else now - proposal.frame_t_ms,
            inference_ms=None if proposal is None else proposal.inference_ms,
            candidates=tuple((c.score, c.path_xy) for c in candidates),
            chosen_index=choice.index,
        )

    def _localize(self, sensors: Sensors) -> Pose | None:
        """Stage 2. Trusts the phone's fix and heading as given; no filtering yet."""
        if sensors.lat is None or sensors.lon is None or sensors.heading_deg is None:
            return None
        if self.origin is None:
            self.origin = (sensors.lat, sensors.lon)
        east, north = to_local(sensors.lat, sensors.lon, self.origin)
        return Pose(
            east,
            north,
            sensors.heading_deg,
            sensors.speed_mps or 0.0,
            sensors.yaw_rate_radps or 0.0,
        )

    def _follow_chair(self, chair: ChairStatus) -> None:
        """The joystick always wins: the chair says it is manual, and the trip follows."""
        manual = chair.mode == "manual" or chair.joystick_active
        if manual:
            self.mode = "manual"
        elif self.mode == "manual":
            # Self-driving does not resume by itself after the rider lets go.
            self.mode = "ready" if self._has_trip() else "idle"

    def _goal(self, pose: Pose | None) -> tuple[Goal | None, float]:
        """Stage 3. What the model should aim for, and how far is left."""
        if self.instruction is not None:
            return Goal(instruction=self.instruction), 0.0
        if self.route is None or pose is None:
            return None, 0.0
        self.progress_m = self.route.project(pose.east, pose.north, self.progress_m)
        remaining = self.route.length_m - self.progress_m
        end = self.route.point_at(self.route.length_m)
        to_end = float(np.hypot(end[0] - pose.east, end[1] - pose.north))
        if self.mode == "driving" and max(remaining, to_end) <= self.config.arrive_radius_m:
            self.mode = "arrived"
        target = self.route.point_at(self.progress_m + self.config.goal_lookahead_m)
        ahead, left = world_to_chair(target, pose.east, pose.north, pose.heading_deg)[0]
        return Goal(point_xy=(float(ahead), float(left))), remaining

    def _gate(
        self, sensors: Sensors, chair: ChairStatus, proposal: Proposal | None, now: float
    ) -> str | None:
        """Stage 1. Why the chair must not move right now, or None."""
        config = self.config
        if chair.estop:
            return "Emergency stop pressed"
        if self._frame_t_ms is None or now - self._frame_t_ms > config.frame_max_age_ms:
            return "Camera feed lost"
        if self.route is not None and (
            sensors.fix_t_ms is None or now - sensors.fix_t_ms > config.fix_max_age_ms
        ):
            return "Position lost"
        if proposal is None:
            return "Waiting for driver model"
        if now - proposal.frame_t_ms > config.model_max_age_ms:
            return "Driver model not responding"
        return None

    def _engage_blocked(self, chair: ChairStatus, gate_reason: str | None) -> str | None:
        if self.mode == "driving":
            return None
        if not self._has_trip():
            return "Set a destination first"
        if chair.mode == "manual" or chair.joystick_active:
            return "Release the joystick"
        return gate_reason
