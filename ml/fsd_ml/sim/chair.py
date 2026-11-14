"""A virtual chair that obeys drive commands the way the real one is meant to.

It follows docs/outputs.md: it ignores out-of-date commands, stops when the
newest command expires, and the joystick and e-stop override everything.
"""

from __future__ import annotations

import math

from fsd_ml.pipeline.types import ChairStatus, DriveCommand


class SimChair:
    def __init__(
        self,
        east: float = 0.0,
        north: float = 0.0,
        heading_deg: float = 0.0,
        max_accel_mps2: float = 1.5,
    ) -> None:
        self.east = east
        self.north = north
        self.heading_deg = heading_deg
        self.speed_mps = 0.0
        self.yaw_rate_radps = 0.0
        self.joystick_active = False
        self.estop = False
        self.battery_pct = 80.0
        self._max_accel = max_accel_mps2
        self._command: DriveCommand | None = None

    def receive(self, command: DriveCommand) -> None:
        if self._command is None or command.seq > self._command.seq:
            self._command = command

    def status(self) -> ChairStatus:
        return ChairStatus(
            mode="manual" if self.joystick_active else "auto",
            joystick_active=self.joystick_active,
            estop=self.estop,
            battery_pct=self.battery_pct,
            applied_seq=self._command.seq if self._command else None,
        )

    def step(self, now_ms: float, dt_s: float) -> None:
        command = self._command
        expired = command is None or now_ms - command.t_ms > command.ttl_ms
        if expired or self.joystick_active or self.estop:
            # In the simulator a rider holding the joystick holds the chair still.
            target_speed, target_turn = 0.0, 0.0
        else:
            target_speed, target_turn = command.v_mps, command.w_radps

        step = self._max_accel * dt_s
        self.speed_mps += max(-step, min(step, target_speed - self.speed_mps))
        if self.estop:
            self.speed_mps = 0.0
        self.yaw_rate_radps = target_turn

        # Heading is clockwise; a positive turn rate turns left.
        self.heading_deg = (self.heading_deg - math.degrees(target_turn * dt_s)) % 360.0
        heading = math.radians(self.heading_deg)
        self.east += self.speed_mps * math.sin(heading) * dt_s
        self.north += self.speed_mps * math.cos(heading) * dt_s
