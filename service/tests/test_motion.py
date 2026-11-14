"""Speed and turn-rate estimator, driven by synthetic iPhone IMU and GPS samples.

iOS conventions (see docs/phone-protocol.md): accelerationIncludingGravity reads
the gravity vector itself, pointing at the ground, so a phone at rest in a
portrait mount (top of the screen up) reads y = -9.8. A true acceleration ``a``
reads as ``gravity - a``. Rotation rates are right-handed about device x, y, z.
"""

from __future__ import annotations

import math

import pytest

from drive_service.motion import (
    IMU_GAP_MS,
    MAX_IMU_COAST_MS,
    STILL_ACCEL_SPREAD_MPS2,
    STILL_HOLD_MOVING_MS,
    STILL_HOLD_MS,
    STILL_LINEAR_ACCEL_MPS2,
    MotionState,
    motion_record,
    to_device_xyz,
    update_gps,
    update_imu,
)
from drive_service.protocol import Gps, Imu
from helpers import gps_dict

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
        state, result = update_imu(state, _batch(rows, batch_t), legacy_axes=False)
        rows_out.extend(result.rows)
    return state, rows_out


def test_a_new_state_knows_nothing():
    state = MotionState()
    assert state.estimate.speed_mps is None
    assert state.estimate.speed_source is None
    assert state.last_t_ms is None


def test_still_phone_reads_zero_speed_and_source_still():
    state, rows = _run(MotionState(), seconds=1.0)
    assert state.estimate.speed_mps == 0.0
    assert state.estimate.speed_source == "still"
    assert state.estimate.yaw_rate_dps == pytest.approx(0.0, abs=1e-9)
    assert state.estimate.accel_fwd_mps2 == pytest.approx(0.0, abs=1e-9)
    assert rows[-1].speed_mps == 0.0


def test_speed_is_unknown_until_still_long_enough():
    state, _ = _run(MotionState(), seconds=STILL_HOLD_MS / 1000 / 2)
    assert state.estimate.speed_mps is None


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


def test_a_sustained_push_is_not_absorbed_as_tilt_or_read_as_still():
    # Longer than STILL_HOLD_MS plus the old 0.5 s gravity leak, short of the
    # IMU-only coast limit: the true speed at the end is 2.5 m/s.
    push_s = 2.5
    assert push_s * 1000 < MAX_IMU_COAST_MS
    state, _ = _run(MotionState(), seconds=1.0)
    state, rows = _run(state, seconds=push_s, t=T0 + 1000.0, accel_fwd=1.0)
    speeds = [row.speed_mps for row in rows]
    # A still reset would snap the speed back to 0 part way through.
    assert all(speed is not None and speed > 0.0 for speed in speeds)
    assert _non_decreasing(speeds)
    assert speeds[-1] == pytest.approx(push_s, rel=SPEED_TOLERANCE)
    # The slow gravity pull lets e^(-2.5 s / 10 s) of the push through by the end.
    assert rows[-1].accel_fwd_mps2 > 0.75
    assert state.estimate.speed_source == "imu"


def _roll_at_steady_speed(coast_s: float) -> tuple[MotionState, list]:
    """Still, 1 s at 1 m/s^2, then ``coast_s`` at a steady speed on a smooth floor."""
    state, _ = _run(MotionState(), seconds=1.0)
    state, _ = _run(state, seconds=1.0, t=T0 + 1000.0, accel_fwd=1.0)
    return _run(state, seconds=coast_s, t=state.last_t_ms + SAMPLE_MS)


def test_a_steady_roll_without_gps_speed_is_not_read_as_still():
    # Indoors GPS reports no speed, and a steady roll on a smooth floor looks
    # still to an IMU. Moving speed holds off the still reset for longer.
    coast_s = (STILL_HOLD_MS + 500.0) / 1000
    assert coast_s * 1000 < STILL_HOLD_MOVING_MS
    state, rows = _roll_at_steady_speed(coast_s)
    assert all(row.speed_mps > 0.7 for row in rows)
    assert state.estimate.speed_source == "imu"


def test_a_roll_that_settles_long_enough_is_still():
    state, _ = _roll_at_steady_speed((STILL_HOLD_MOVING_MS + 200.0) / 1000)
    assert state.estimate.speed_mps == 0.0
    assert state.estimate.speed_source == "still"


def test_braking_never_makes_speed_negative():
    state, _ = _run(MotionState(), seconds=1.0)
    state, rows = _run(state, seconds=0.5, t=T0 + 1000.0, accel_fwd=-2.0)
    assert all(row.speed_mps >= 0.0 for row in rows)


def test_gps_fix_pulls_speed_toward_it():
    state, _ = _run(MotionState(), seconds=1.0)
    fix_t = state.last_t_ms
    pulled = update_gps(state, Gps.model_validate(gps_dict(t=fix_t, fix_t=fix_t, speed_mps=2.0)))
    assert 1.0 < pulled.estimate.speed_mps < 2.0
    assert pulled.estimate.speed_source == "gps"
    # Between fixes the IMU carries it.
    after, _ = _run(pulled, seconds=0.1, t=fix_t + SAMPLE_MS, accel_fwd=0.5)
    assert after.estimate.speed_source == "imu"


def test_gps_fix_sets_unknown_speed_outright():
    state = update_gps(MotionState(), Gps.model_validate(gps_dict(speed_mps=1.5)))
    assert state.estimate.speed_mps == 1.5
    assert state.estimate.speed_source == "gps"


def test_gps_fix_without_speed_leaves_the_estimate_alone():
    state, _ = _run(MotionState(), seconds=1.0)
    fix = Gps.model_validate(gps_dict(t=state.last_t_ms, fix_t=state.last_t_ms, speed_mps=None))
    assert update_gps(state, fix).estimate == state.estimate


def test_gps_stopping_releases_the_still_reset():
    moving = Gps.model_validate(gps_dict(t=T0, fix_t=T0, speed_mps=1.2))
    stopped = Gps.model_validate(gps_dict(t=T0 + 1.0, fix_t=T0 + 1.0, speed_mps=None))
    state = update_gps(update_gps(MotionState(), moving), stopped)
    # Speed is still 1.2 from the moving fix (a null speed is also what GPS
    # reports indoors), so the stop must last the longer moving hold.
    state, _ = _run(state, seconds=(STILL_HOLD_MOVING_MS + 200.0) / 1000, t=T0 + 2.0)
    assert state.estimate.speed_mps == 0.0
    assert state.estimate.speed_source == "still"


def test_a_resent_or_old_fix_is_ignored():
    state, _ = _run(MotionState(), seconds=1.0)
    t = state.last_t_ms
    fix = Gps.model_validate(gps_dict(t=t, fix_t=t, speed_mps=2.0))
    once = update_gps(state, fix)
    assert update_gps(once, fix) == once
    old = Gps.model_validate(gps_dict(t=t, fix_t=t - 10_000.0, speed_mps=3.0))
    assert update_gps(state, old) == state


def test_absurd_gps_speed_is_ignored():
    state = update_gps(MotionState(), Gps.model_validate(gps_dict(speed_mps=500.0)))
    assert state == MotionState()


def test_moving_by_gps_blocks_the_still_reset():
    # Smooth floor at a steady speed: no rotation, little vibration, but GPS says moving.
    state = update_gps(MotionState(), Gps.model_validate(gps_dict(t=T0, fix_t=T0, speed_mps=1.2)))
    state, _ = _run(state, seconds=1.0)
    assert state.estimate.speed_mps == pytest.approx(1.2)
    assert state.estimate.speed_source == "imu"


def test_vibration_is_not_mistaken_for_standing_still():
    state, _ = _run(MotionState(), seconds=1.0, jitter=0.4)
    assert state.estimate.speed_mps is None


def test_gentle_vibration_with_a_wide_spread_is_not_still():
    # Each sample is under the linear limit, but together they spread too far.
    gentle = 0.9 * STILL_LINEAR_ACCEL_MPS2
    assert 2 * gentle > STILL_ACCEL_SPREAD_MPS2
    state, _ = _run(MotionState(), seconds=1.0)
    assert state.estimate.speed_source == "still"
    state, _ = _run(state, seconds=1.0, t=T0 + 1000.0, jitter=gentle)
    assert state.estimate.speed_source == "imu"


def test_weak_gravity_gives_no_up_and_no_turn_rate():
    # Shaken or falling: the gravity estimate is too weak to say which way is up.
    rows = [[0.0, 0.0, -2.0, 0.0, 0.0, 30.0, 0.0]]
    _, result = update_imu(MotionState(), _batch(rows), legacy_axes=False)
    assert result.rows[0].yaw_rate_dps is None
    assert result.rows[0].accel_fwd_mps2 is None


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
    state, result = update_imu(
        state, _batch([[0.0, *left_reading, 0.0, 0.0, 0.0]], T0 + 1000.0), legacy_axes=False
    )
    assert result.rows[0].accel_lat_mps2 == pytest.approx(1.0, rel=0.05)


def test_camera_pointing_straight_up_has_no_forward():
    # Phone lying face down: back camera looks at the sky, so forward is undefined.
    rows = [[i * SAMPLE_MS, 0.0, 0.0, G, 0.0, 0.0, 0.0] for i in range(6)]
    _, result = update_imu(MotionState(), _batch(rows), legacy_axes=False)
    assert result.rows[-1].accel_fwd_mps2 is None
    assert result.rows[-1].accel_lat_mps2 is None
    assert result.rows[-1].yaw_rate_dps == pytest.approx(0.0)


def test_a_gap_in_samples_resets_to_unknown():
    state, _ = _run(MotionState(), seconds=1.0)
    assert state.estimate.speed_mps == 0.0
    later = state.last_t_ms + IMU_GAP_MS + 100.0
    state, _ = update_imu(state, _batch([_sample(0.0, PORTRAIT_UP)], later), legacy_axes=False)
    assert state.estimate.speed_mps is None
    assert state.estimate.speed_source is None


@pytest.mark.parametrize(
    "bad",
    [
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # missing accelerometer reads all zeros
        [0.0, 0.0, -G, 900.0, 0.0, 0.0, 0.0],  # beyond any phone accelerometer
        [0.0, 0.0, -G, 0.0, 5000.0, 0.0, 0.0],  # beyond any phone gyro
    ],
)
def test_bad_samples_are_ignored_but_keep_their_row(bad):
    state, _ = _run(MotionState(), seconds=1.0)
    bad_row = [0.0, *bad[1:]]
    after, result = update_imu(state, _batch([bad_row], state.last_t_ms + 5.0), legacy_axes=False)
    assert after == state
    assert len(result.rows) == 1
    assert result.rows[0].dt_ms == 0.0
    assert result.rows[0].speed_mps is None
    assert result.newest_t_ms is None


def test_a_sample_from_the_past_is_ignored():
    state, _ = _run(MotionState(), seconds=1.0)
    after, result = update_imu(
        state, _batch([_sample(0.0, PORTRAIT_UP)], state.last_t_ms - 50.0), legacy_axes=False
    )
    assert after == state
    assert result.rows[0].yaw_rate_dps is None


def test_rows_line_up_with_imu_samples():
    rows = [_sample(dt, PORTRAIT_UP) for dt in (0.0, 16.0, 33.0)]
    _, result = update_imu(MotionState(), _batch(rows), legacy_axes=False)
    assert [row.dt_ms for row in result.rows] == [0.0, 16.0, 33.0]
    assert result.newest_t_ms == T0 + 33.0


def test_legacy_axes_are_remapped_to_device_xyz():
    # Old pages wrote [about y, about z, about x] into rx, ry, rz.
    legacy = (0.0, 1.0, 2.0, 3.0, 10.0, 20.0, 30.0)
    assert to_device_xyz(legacy, legacy_axes=True) == (0.0, 1.0, 2.0, 3.0, 30.0, 10.0, 20.0)
    assert to_device_xyz(legacy, legacy_axes=False) == legacy


def test_legacy_batch_turning_left_reads_positive():
    # Portrait turn left is rotation about device y; an old page put y in the rx column.
    rows = [[i * SAMPLE_MS, 0.0, -G, 0.0, 25.0, 0.0, 0.0] for i in range(6)]
    state, _ = update_imu(MotionState(), _batch(rows), legacy_axes=True)
    assert state.estimate.yaw_rate_dps == pytest.approx(25.0)


def test_motion_record_matches_the_shared_contract():
    rows = [_sample(dt, PORTRAIT_UP) for dt in (0.0, 16.0)]
    state, result = update_imu(MotionState(), _batch(rows), legacy_axes=False)
    record = motion_record(_batch(rows), state, result)
    assert record["v"] == 1
    assert record["type"] == "motion"
    assert record["t"] == T0
    assert record["speed_source"] == state.estimate.speed_source
    assert len(record["samples"]) == 2
    assert record["samples"][1][0] == 16.0
    assert all(len(row) == 5 for row in record["samples"])
    assert all(value is None or math.isfinite(value) for row in record["samples"] for value in row)


def test_imu_only_speed_expires_without_an_anchor():
    # Turning keeps it from ever looking still, and no GPS speed arrives.
    state, _ = _run(MotionState(), seconds=1.0)
    state, _ = _run(
        state, seconds=(MAX_IMU_COAST_MS + 500.0) / 1000, t=T0 + 1000.0, gyro=(0, 20, 0)
    )
    assert state.estimate.speed_mps is None
    assert state.estimate.speed_source is None


def test_gps_fixes_keep_imu_speed_alive():
    state, _ = _run(MotionState(), seconds=1.0)
    t = T0 + 1000.0
    for _ in range(4):
        fix = Gps.model_validate(gps_dict(t=t, fix_t=t, speed_mps=1.0))
        state = update_gps(state, fix)
        state, _ = _run(state, seconds=1.0, t=t + SAMPLE_MS, gyro=(0, 20, 0))
        t = state.last_t_ms
    # Four blends at GPS_BLEND_GAIN from 0: 1 - 0.3**4.
    assert state.estimate.speed_mps == pytest.approx(1.0, abs=0.01)
    assert state.estimate.speed_source == "imu"


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
    _, result = update_imu(MotionState(), _batch(rows), legacy_axes=False)
    leaks = [abs(row.accel_fwd_mps2) for row in result.rows if row.accel_fwd_mps2 is not None]
    assert max(leaks) < 0.3
