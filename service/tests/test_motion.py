"""Speed and turn-rate estimator, driven by synthetic iPhone IMU and GPS samples.

iOS conventions (see drive_service/motion.py): accelerationIncludingGravity reads
the gravity vector itself, pointing at the ground, so a phone at rest in a
portrait mount (top of the screen up) reads y = -9.8. A true acceleration ``a``
reads as ``gravity - a``. Rotation rates are right-handed about device x, y, z.
"""

from __future__ import annotations

import math

import pytest

from drive_service.motion import (
    MotionState,
    update_imu,
)
from drive_service.protocol import Imu

G = 9.80665
T0 = 1_700_000_000_000.0
SAMPLE_MS = 16.0
# IMU-only speed may under-read a steady push a little as it leaks into gravity.
SPEED_TOLERANCE = 0.15

# Unit "up" in device axes for the two mounts tested, and the device axis the
# back camera looks along (-z) is forward in both.
PORTRAIT_UP = (0.0, 1.0, 0.0)
LANDSCAPE_UP = (1.0, 0.0, 0.0)


def _sample(
    dt_ms: float,
    up: tuple[float, float, float],
    accel_fwd: float = 0.0,
    gyro: tuple[float, float, float] = (0.0, 0.0, 0.0),
    noise: float = 0.0,
) -> list[float]:
    """One device_xyz IMU row for a phone whose camera looks forward along -z."""
    gravity = tuple(-G * axis for axis in up)
    # True acceleration along forward (-z) reads as gravity - a.
    true_accel = (0.0, 0.0, -accel_fwd)
    reading = [g - a for g, a in zip(gravity, true_accel, strict=True)]
    reading[0] += noise
    return [dt_ms, *reading, *gyro]


def _batch(rows: list[list[float]], t: float = T0) -> Imu:
    return Imu.model_validate({"v": 1, "type": "imu", "t": t, "samples": rows})


def _run(
    state: MotionState,
    seconds: float,
    up=PORTRAIT_UP,
    t: float = T0,
    accel_fwd: float = 0.0,
    gyro=(0.0, 0.0, 0.0),
    jitter: float = 0.0,
) -> tuple[MotionState, list]:
    """Feed ``seconds`` of 100 ms batches. Returns the state and every row produced."""
    rows_out = []
    count = round(seconds * 1000 / SAMPLE_MS)
    per_batch = 6
    for start in range(0, count, per_batch):
        batch_t = t + start * SAMPLE_MS
        rows = [
            _sample(
                i * SAMPLE_MS,
                up,
                accel_fwd,
                gyro,
                noise=jitter if (start + i) % 2 else -jitter,
            )
            for i in range(min(per_batch, count - start))
        ]
        state, result = update_imu(state, _batch(rows, batch_t))
        rows_out.extend(result.rows)
    return state, rows_out


def _non_decreasing(values: list[float]) -> bool:
    return all(later >= earlier for earlier, later in zip(values, values[1:], strict=False))


def test_constant_forward_acceleration_makes_speed_grow():
    # 1 s at 1 m/s^2 from rest: the true speed is 1.0 m/s.
    state, _ = _run(MotionState(), seconds=1.0)
    state, rows = _run(state, seconds=1.0, t=T0 + 1000.0, accel_fwd=1.0)
    speeds = [row.speed_mps for row in rows]
    assert speeds[-1] == pytest.approx(1.0, rel=SPEED_TOLERANCE)
    assert _non_decreasing(speeds)
    assert state.estimate.speed_source == "imu"
    assert rows[0].accel_fwd_mps2 == pytest.approx(1.0, rel=0.05)


@pytest.mark.parametrize(("up", "gyro"), [(PORTRAIT_UP, (0, 20, 0)), (LANDSCAPE_UP, (20, 0, 0))])
def test_turning_left_is_a_positive_yaw_rate_for_any_mount(up, gyro):
    state, rows = _run(MotionState(), seconds=0.5, up=up, gyro=gyro)
    assert state.estimate.yaw_rate_dps == pytest.approx(20.0)
    assert rows[-1].yaw_rate_dps == pytest.approx(20.0)


def test_turning_right_is_negative():
    state, _ = _run(MotionState(), seconds=0.5, gyro=(0, -15, 0))
    assert state.estimate.yaw_rate_dps == pytest.approx(-15.0)


def test_lateral_acceleration_to_the_left_is_positive():
    # Portrait: forward is -z, up is +y, so left is up x forward = -x.
    left_reading = [0.0, -G, 0.0]
    left_reading[0] -= -1.0  # true a = (-1, 0, 0) reads as gravity - a
    state, _ = _run(MotionState(), seconds=1.0)
    state, result = update_imu(state, _batch([[0.0, *left_reading, 0.0, 0.0, 0.0]], T0 + 1000.0))
    assert result.rows[0].accel_lat_mps2 == pytest.approx(1.0, rel=0.05)


def test_tilting_the_phone_is_not_read_as_acceleration():
    # Pitch the portrait phone forward at 90 deg/s for half a second (about device x)
    # with no real acceleration: gravity rotates in the device frame and the
    # gyro-aided estimate follows it, so almost nothing leaks into accel_fwd.
    rate_dps = 90.0
    rows = []
    for i in range(32):
        angle = math.radians(rate_dps * i * SAMPLE_MS / 1000)
        # Gravity seen by a phone pitched by ``angle`` about x (right-handed).
        gravity = (0.0, -G * math.cos(angle), G * math.sin(angle))
        rows.append([i * SAMPLE_MS, *gravity, rate_dps, 0.0, 0.0])
    _, result = update_imu(MotionState(), _batch(rows))
    leaks = [abs(row.accel_fwd_mps2) for row in result.rows if row.accel_fwd_mps2 is not None]
    assert max(leaks) < 0.3
