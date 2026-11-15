"""Speed, turn rate and acceleration of the chair from the phone's IMU and GPS.

Pure functions: a frozen ``MotionState`` goes in, a new one comes out.

iOS conventions (verified against WebKit's WebCoreMotionManager and Apple's
CoreMotion docs):

* Device axes: x to the right of the screen, y toward its top, z out of the
  screen toward the viewer. The back camera looks along -z, so -z is "forward".
* Rotation rates are right-handed about device x, y, z in deg/s, in that order
  (the hello's ``imu_axes: "device_xyz"``).
* ``accelerationIncludingGravity`` is CoreMotion's (user acceleration + gravity)
  times 9.80665, and CoreMotion's gravity points TOWARD the ground (a phone lying
  face up reads z = -9.8). So the low-passed reading G points down, up is -G/|G|,
  and a true acceleration a reads as G - a: ``a = IOS_ACCEL_SIGN * (reading - G)``.
  Device check: lay the phone flat, face up; az must read about -9.8.

Method:

* Gravity: a complementary filter. Each sample the estimate is turned by the
  gyro (a world-fixed vector seen from the phone moves as dG/dt = -w x G), then
  pulled toward the reading with time constant ``GRAVITY_TIME_CONSTANT_S``. The
  gyro step keeps tilting (bumps, slopes, a hand-held phone) out of the linear
  acceleration, so the pull only has to remove gyro drift and can be slow. A
  fast pull would absorb a steady push as tilt within a second: the push would
  vanish from the linear acceleration and the chair would look still. What
  leaks in is small (about 12% of the speed gained over a 2.5 s push); GPS
  pulls speed back about once a second. Replaying the first iPhone recording
  confirmed the axis order and sign: propagating with right-handed x, y, z
  rates tracked the accelerometer best (20 deg error vs 32-66 deg otherwise).
* Yaw rate: rotation rate dotted with unit up, so it works for any mount.
  Positive = turning left (counter-clockwise seen from above).
* Yaw: the yaw rate summed sample by sample over the phone's own sample times, so
  how messages are batched or delayed on the way here does not change it. It is
  how far the chair has turned since the phone connected, not a compass heading.
  A resting gyro reads a small steady rate (its bias); that is learned whenever
  the phone is still and taken off every sample, so standing does not drift.
* Forward: the camera axis (-z) projected onto the horizontal plane; left is
  up x forward. Accelerations are projected onto both.
* Speed: integrate forward acceleration between GPS fixes (never below 0), blend
  toward each GPS speed (``GPS_BLEND_GAIN``), and snap to 0 once the phone has
  been still for ``STILL_HOLD_MS``. A smooth floor at a steady speed looks still
  to an IMU, so while the speed estimate says moving the hold is the longer
  ``STILL_HOLD_MOVING_MS``, and while GPS says moving there is no reset at all.
  Unknown until a still period or a GPS speed gives a starting point; unknown
  again after a gap in samples, or after ``MAX_IMU_COAST_MS`` of integrating
  with neither (drift grows fast).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Literal

from drive_service.protocol import Gps, Imu, ImuSample
from drive_service.vec3 import Vec3, cross, dot, high, low, norm, scale, sub

MS_PER_S = 1000.0
MOTION_RECORD_VERSION = 1

# a = IOS_ACCEL_SIGN * (accelerationIncludingGravity - gravity): iOS reports the
# negative of specific force (W3C browsers report +specific force, sign +1).
IOS_ACCEL_SIGN = -1.0
# Gyro drift on a bias-corrected iPhone gyro is a few hundredths of a deg/s, so
# a slow pull keeps the tilt error well under a degree.
GRAVITY_TIME_CONSTANT_S = 10.0
# The back camera's optical axis in device axes.
CAMERA_AXIS: Vec3 = (0.0, 0.0, -1.0)

# Samples further apart than this mean the stream stopped: start again from unknown.
IMU_GAP_MS = 500.0
# Integrated speed drifts; past this long since the last GPS speed or still
# period it is reported as unknown. Two missed 1 Hz fixes still fit.
MAX_IMU_COAST_MS = 3000.0
# Still: rotation below STILL_GYRO_DPS, linear acceleration below
# STILL_LINEAR_ACCEL_MPS2, and the raw reading within STILL_ACCEL_SPREAD_MPS2 on
# every axis, all held for STILL_HOLD_MS. Wheelchair vibration on pavement is
# well above the spread; an iPhone at rest is well below it.
STILL_HOLD_MS = 500.0
# While the speed estimate is above STILL_MOVING_SPEED_MPS the chair may be
# rolling steadily on a smooth floor, so stillness must last this long instead.
STILL_HOLD_MOVING_MS = 1500.0
STILL_MOVING_SPEED_MPS = 0.3
STILL_GYRO_DPS = 2.0
STILL_LINEAR_ACCEL_MPS2 = 0.25
STILL_ACCEL_SPREAD_MPS2 = 0.4
# How fast the gyro's resting yaw rate is learned once the phone is held still.
GYRO_BIAS_TIME_CONSTANT_S = 2.0

# Each GPS speed moves the estimate this fraction of the way to it.
GPS_BLEND_GAIN = 0.7
# A fix older than the newest IMU sample by more than this is history, not news.
GPS_MAX_LAG_MS = 2000.0
# A fix faster than this says the chair is moving, which blocks the still reset
# for GPS_MOVING_HOLD_MS after the fix.
GPS_MOVING_MPS = 0.3
GPS_MOVING_HOLD_MS = 2000.0
MAX_GPS_SPEED_MPS = 50.0
MAX_SPEED_MPS = 15.0

# Sanity limits on raw samples; anything outside is ignored. A missing
# accelerometer reads all zeros; phone sensors top out near 16 g and 2000 deg/s.
MIN_ACCEL_NORM_MPS2 = 1.0
MAX_ACCEL_NORM_MPS2 = 160.0
MAX_ROTATION_DPS = 2000.0
# Gravity weaker than this (free fall, a bad filter start) gives no "up".
MIN_GRAVITY_MPS2 = 5.0
# The camera axis must keep this much of its length once flattened, else the
# phone points at the sky or the ground and "forward" is undefined.
MIN_FORWARD_NORM = 0.2

type SpeedSource = Literal["gps", "imu", "still"]


@dataclass(frozen=True, slots=True)
class MotionEstimate:
    speed_mps: float | None = None
    speed_source: SpeedSource | None = None
    yaw_rate_dps: float | None = None
    # Turned since the phone connected, positive left, bias taken off. Not wrapped.
    yaw_deg: float | None = None
    accel_fwd_mps2: float | None = None
    accel_lat_mps2: float | None = None


@dataclass(frozen=True, slots=True)
class MotionRow:
    """One output row per IMU sample; ``dt_ms`` is that sample's."""

    dt_ms: float
    speed_mps: float | None = None
    yaw_rate_dps: float | None = None
    accel_fwd_mps2: float | None = None
    accel_lat_mps2: float | None = None

    def as_list(self) -> list[float | None]:
        return [
            self.dt_ms,
            self.speed_mps,
            self.yaw_rate_dps,
            self.accel_fwd_mps2,
            self.accel_lat_mps2,
        ]


@dataclass(frozen=True, slots=True)
class StillWindow:
    start_ms: float
    low: Vec3
    high: Vec3


@dataclass(frozen=True, slots=True)
class MotionState:
    estimate: MotionEstimate = MotionEstimate()
    # Low-passed accelerationIncludingGravity in device axes (points down).
    gravity: Vec3 | None = None
    # Phone time of the newest sample used.
    last_t_ms: float | None = None
    still: StillWindow | None = None
    gps_fix_t_ms: float | None = None
    gps_moving: bool = False
    # Phone time speed was last pinned by a GPS speed or a still period.
    anchor_t_ms: float | None = None
    # The running yaw, and the gyro's resting yaw rate as last learned.
    yaw_deg: float = 0.0
    yaw_bias_dps: float = 0.0


@dataclass(frozen=True, slots=True)
class BatchResult:
    rows: tuple[MotionRow, ...]
    # Phone time of the newest sample used, or None if every sample was ignored.
    newest_t_ms: float | None


@dataclass(frozen=True, slots=True)
class _Axes:
    up: Vec3
    forward: Vec3 | None
    left: Vec3 | None


def update_imu(state: MotionState, batch: Imu) -> tuple[MotionState, BatchResult]:
    """Run every sample of a batch through the estimator, one output row each."""
    rows: list[MotionRow] = []
    newest: float | None = None
    for sample in batch.samples:
        t_ms = batch.t + sample[0]
        if not _usable(sample) or (state.last_t_ms is not None and t_ms < state.last_t_ms):
            rows.append(MotionRow(dt_ms=sample[0]))
            continue
        state = _step(state, sample, t_ms)
        rows.append(_row(sample[0], state.estimate))
        newest = t_ms
    return state, BatchResult(rows=tuple(rows), newest_t_ms=newest)


def update_gps(state: MotionState, fix: Gps) -> MotionState:
    """Blend a new GPS speed in. Resent, stale or absurd fixes change nothing."""
    fix_t = min(fix.fix_t, fix.t)
    if fix.speed_mps is not None and fix.speed_mps > MAX_GPS_SPEED_MPS:
        return state
    if state.gps_fix_t_ms is not None and fix_t <= state.gps_fix_t_ms:
        return state
    if state.last_t_ms is not None and fix_t < state.last_t_ms - GPS_MAX_LAG_MS:
        return state
    if fix.speed_mps is None:
        # The phone reports no speed when stopped: no number to blend, but GPS
        # no longer says the chair is moving.
        return replace(state, gps_fix_t_ms=fix_t, gps_moving=False)
    current = state.estimate.speed_mps
    speed = (
        fix.speed_mps if current is None else current + GPS_BLEND_GAIN * (fix.speed_mps - current)
    )
    return replace(
        state,
        estimate=replace(state.estimate, speed_mps=speed, speed_source="gps"),
        gps_fix_t_ms=fix_t,
        gps_moving=fix.speed_mps > GPS_MOVING_MPS,
        anchor_t_ms=fix_t,
    )


def motion_record(batch: Imu, state: MotionState, result: BatchResult) -> dict[str, Any]:
    """The derived sensors.jsonl line for one IMU batch (the recorder adds recv_ms)."""
    return {
        "v": MOTION_RECORD_VERSION,
        "type": "motion",
        "t": batch.t,
        "speed_source": state.estimate.speed_source,
        "samples": [row.as_list() for row in result.rows],
    }


def _usable(sample: ImuSample) -> bool:
    accel_norm = norm((sample[1], sample[2], sample[3]))
    gyro_norm = norm((sample[4], sample[5], sample[6]))
    return MIN_ACCEL_NORM_MPS2 <= accel_norm <= MAX_ACCEL_NORM_MPS2 and (
        gyro_norm <= MAX_ROTATION_DPS
    )


def _step(state: MotionState, sample: ImuSample, t_ms: float) -> MotionState:
    if state.last_t_ms is not None and t_ms - state.last_t_ms > IMU_GAP_MS:
        # What was turned before the gap still was; what was turned during it is lost.
        state = MotionState(yaw_deg=state.yaw_deg, yaw_bias_dps=state.yaw_bias_dps)
    reading: Vec3 = (sample[1], sample[2], sample[3])
    gyro: Vec3 = (sample[4], sample[5], sample[6])
    dt_s = 0.0 if state.last_t_ms is None else (t_ms - state.last_t_ms) / MS_PER_S
    prior = reading if state.gravity is None else _rotate(state.gravity, gyro, dt_s)
    linear = scale(sub(reading, prior), IOS_ACCEL_SIGN)
    axes = _chair_axes(prior)
    still = _update_still(state.still, reading, gyro, linear, t_ms)
    accel_fwd = _project(linear, axes.forward if axes else None)
    speed, source, anchor = _speed(state, accel_fwd, dt_s, still, t_ms)
    yaw_rate = dot(gyro, axes.up) if axes else None
    yaw, bias = state.yaw_deg, state.yaw_bias_dps
    if yaw_rate is not None:
        if still is not None and t_ms - still.start_ms >= STILL_HOLD_MS:
            bias += (yaw_rate - bias) * dt_s / (GYRO_BIAS_TIME_CONSTANT_S + dt_s)
        yaw += (yaw_rate - bias) * dt_s
    estimate = MotionEstimate(
        speed_mps=speed,
        speed_source=source,
        yaw_rate_dps=yaw_rate,
        yaw_deg=yaw if yaw_rate is not None else None,
        accel_fwd_mps2=accel_fwd,
        accel_lat_mps2=_project(linear, axes.left if axes else None),
    )
    return replace(
        state,
        estimate=estimate,
        gravity=_low_pass(prior, reading, dt_s),
        last_t_ms=t_ms,
        still=still,
        anchor_t_ms=anchor,
        yaw_deg=yaw,
        yaw_bias_dps=bias,
    )


def _rotate(gravity: Vec3, gyro_dps: Vec3, dt_s: float) -> Vec3:
    """Turn a world-fixed vector as the phone turns: dG/dt = -w x G (one Euler step)."""
    omega = scale(gyro_dps, math.radians(1.0))
    return sub(gravity, scale(cross(omega, gravity), dt_s))


def _low_pass(prior: Vec3, reading: Vec3, dt_s: float) -> Vec3:
    gain = dt_s / (GRAVITY_TIME_CONSTANT_S + dt_s)
    return (
        prior[0] + gain * (reading[0] - prior[0]),
        prior[1] + gain * (reading[1] - prior[1]),
        prior[2] + gain * (reading[2] - prior[2]),
    )


def _chair_axes(gravity: Vec3) -> _Axes | None:
    """Unit up, forward and left in device axes, from the gravity estimate."""
    strength = norm(gravity)
    if strength < MIN_GRAVITY_MPS2:
        return None
    up = scale(gravity, -1.0 / strength)
    flattened = sub(CAMERA_AXIS, scale(up, dot(CAMERA_AXIS, up)))
    length = norm(flattened)
    if length < MIN_FORWARD_NORM:
        return _Axes(up=up, forward=None, left=None)
    forward = scale(flattened, 1.0 / length)
    return _Axes(up=up, forward=forward, left=cross(up, forward))


def _project(vector: Vec3, axis: Vec3 | None) -> float | None:
    return None if axis is None else dot(vector, axis)


def _update_still(
    window: StillWindow | None, reading: Vec3, gyro: Vec3, linear: Vec3, t_ms: float
) -> StillWindow | None:
    if norm(gyro) >= STILL_GYRO_DPS or norm(linear) >= STILL_LINEAR_ACCEL_MPS2:
        return None
    if window is None:
        return StillWindow(start_ms=t_ms, low=reading, high=reading)
    lowest, highest = low(window.low, reading), high(window.high, reading)
    if max(sub(highest, lowest)) > STILL_ACCEL_SPREAD_MPS2:
        return StillWindow(start_ms=t_ms, low=reading, high=reading)
    return StillWindow(start_ms=window.start_ms, low=lowest, high=highest)


def _speed(
    state: MotionState,
    accel_fwd: float | None,
    dt_s: float,
    still: StillWindow | None,
    t_ms: float,
) -> tuple[float | None, SpeedSource | None, float | None]:
    """New speed, its source, and the anchor time."""
    held_still = still is not None and t_ms - still.start_ms >= _still_hold_ms(state)
    gps_moving = (
        state.gps_moving
        and state.gps_fix_t_ms is not None
        and t_ms - state.gps_fix_t_ms <= GPS_MOVING_HOLD_MS
    )
    if held_still and not gps_moving:
        return 0.0, "still", t_ms
    speed, anchor = state.estimate.speed_mps, state.anchor_t_ms
    if speed is None or anchor is None or t_ms - anchor > MAX_IMU_COAST_MS:
        return None, None, None
    if accel_fwd is not None:
        speed = min(MAX_SPEED_MPS, max(0.0, speed + accel_fwd * dt_s))
    return speed, "imu", anchor


def _still_hold_ms(state: MotionState) -> float:
    speed = state.estimate.speed_mps
    moving = speed is not None and speed > STILL_MOVING_SPEED_MPS
    return STILL_HOLD_MOVING_MS if moving else STILL_HOLD_MS


def _row(dt_ms: float, estimate: MotionEstimate) -> MotionRow:
    return MotionRow(
        dt_ms=dt_ms,
        speed_mps=estimate.speed_mps,
        yaw_rate_dps=estimate.yaw_rate_dps,
        accel_fwd_mps2=estimate.accel_fwd_mps2,
        accel_lat_mps2=estimate.accel_lat_mps2,
    )
