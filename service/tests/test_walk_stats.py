"""Turning a recorded walk's GPS and compass lines into speed, location and heading numbers."""

import json
import time

import pytest

from helpers import heading_dict
from scripts.walk_session import parse_lines
from scripts.walk_stats import analyse, angle_diff_deg, circular_mean_deg
from walks import (
    FIXED_DECLINATION,
    MOVING_MPS,
    at_rest,
    fixed_declination,
    line,
    shifted,
    walk_north,
)

DELIVERY_MS = 800.0


def report_for(messages: list[dict], clock_offset_ms: float | None = 0.0):
    lines = (line(m, recv_ms=m["t"] + DELIVERY_MS) for m in messages)
    return analyse(parse_lines(lines, clock_offset_ms), MOVING_MPS, fixed_declination)


def compass_for(fixes: list[dict], true_deg: float, after_ms: float = 100.0) -> list[dict]:
    magnetic = (true_deg - FIXED_DECLINATION) % 360.0
    return [heading_dict(t=f["fix_t"] + after_ms, magnetic_deg=magnetic) for f in fixes]


def test_angle_difference_wraps_around_north():
    assert angle_diff_deg(350.0, 10.0) == pytest.approx(-20.0)
    assert angle_diff_deg(10.0, 350.0) == pytest.approx(20.0)
    assert angle_diff_deg(180.0, 0.0) == pytest.approx(-180.0)


def test_circular_mean_handles_the_wrap():
    assert abs(circular_mean_deg([179.0, -179.0])) == pytest.approx(180.0, abs=1e-6)
    assert circular_mean_deg([10.0, 20.0]) == pytest.approx(15.0)
    assert circular_mean_deg([]) is None


def test_steady_walk_gives_distance_speed_and_course():
    report = report_for(walk_north(20))

    assert report.fix_count == 21
    assert report.duration_s == pytest.approx(20.0)
    assert report.path_m == pytest.approx(28.0, abs=0.05)
    assert report.straight_m == pytest.approx(28.0, abs=0.05)
    assert report.moving_s == pytest.approx(20.0)
    assert report.interval_moving.median == pytest.approx(1.0)
    assert report.interval_rest is None
    assert report.gap_count == 0
    assert report.reported_speed.median == pytest.approx(1.4)
    assert report.speed_error.median == pytest.approx(0.0, abs=0.01)
    assert report.speed_bias_mps == pytest.approx(0.0, abs=0.01)
    assert report.course_error.median == pytest.approx(0.0, abs=0.01)
    assert report.accuracy.median == pytest.approx(4.5)
    assert report.delivery_ms.median == pytest.approx(DELIVERY_MS)


def test_phone_overstating_speed_shows_as_positive_bias():
    fixes = [{**fix, "speed_mps": 1.6} for fix in walk_north(10)]
    report = report_for(fixes)
    assert report.speed_bias_mps == pytest.approx(0.2, abs=0.01)
    assert report.speed_error.median == pytest.approx(0.2, abs=0.01)


def test_standing_still_adds_no_distance_and_no_speed_check():
    # iOS at rest: speed 0 while the position wanders by a few metres.
    report = report_for(at_rest(120, every_s=1.0, jitter_m=3.0, speed=0.0))
    assert report.path_m == 0.0
    assert report.moving_s == 0.0
    assert report.speed_error is None
    assert report.speed_bias_mps is None


def test_pause_in_a_walk_does_not_skew_distance_or_speed():
    walk = walk_north(20)
    pause = shifted(at_rest(30, every_s=1.0, jitter_m=3.0, speed=0.0), 20.5)
    report = report_for(walk + pause)
    assert report.path_m == pytest.approx(28.0, abs=0.05)
    assert report.speed_bias_mps == pytest.approx(0.0, abs=0.01)


def test_rest_and_moving_intervals_are_kept_apart():
    rest = at_rest(20, every_s=5.0)
    report = report_for(rest + shifted(walk_north(10), 21.0))
    assert report.interval_rest.median == pytest.approx(5.0)
    assert report.interval_moving.median == pytest.approx(1.0)
    # The step out of rest starts on a fix with no speed, so it is not walking yet.
    assert report.moving_s == pytest.approx(10.0)


def test_gap_in_the_stream_is_not_counted_as_moving():
    report = report_for(walk_north(10) + shifted(walk_north(10), 70.0))
    assert report.gap_count == 1
    assert report.gap_s == pytest.approx(60.0)
    assert report.moving_s == pytest.approx(20.0)
    assert report.interval_moving.max == pytest.approx(1.0)


def test_coarse_fixes_are_left_out_of_distance():
    fixes = walk_north(10)
    fixes[0] = {**fixes[0], "accuracy_m": 65.0, "lat": fixes[0]["lat"] - 0.001}
    report = report_for(fixes)
    assert report.path_m == pytest.approx(12.6, abs=0.05)
    assert report.straight_m == pytest.approx(12.6, abs=0.05)
    assert report.accuracy.max == pytest.approx(65.0)


def test_resent_fix_is_counted_once():
    fixes = walk_north(5)
    resent = {**fixes[2], "t": fixes[2]["t"] + 300.0}
    report = report_for(fixes[:3] + [resent] + fixes[3:])
    assert report.fix_count == len(fixes)
    assert report.interval_moving.max == pytest.approx(1.0)


def test_compass_offset_against_gps_course():
    fixes = walk_north(10)
    report = report_for(fixes + compass_for(fixes, true_deg=15.0))
    assert report.compass_pairs == len(fixes)
    assert report.compass_offset_deg == pytest.approx(15.0)
    assert report.compass_error.median == pytest.approx(15.0)
    assert report.declination_deg == pytest.approx(FIXED_DECLINATION)


def test_backwards_compass_reads_as_half_a_turn():
    fixes = walk_north(9)
    compass = [
        heading_dict(t=f["fix_t"], magnetic_deg=(179.0 if i % 2 else 181.0) - FIXED_DECLINATION)
        for i, f in enumerate(fixes)
    ]
    report = report_for(fixes + compass)
    assert abs(report.compass_offset_deg) == pytest.approx(180.0, abs=1e-6)
    assert report.compass_error.median == pytest.approx(179.0)


def test_compass_far_from_any_fix_is_not_paired():
    fixes = walk_north(3)
    compass = [heading_dict(t=fixes[-1]["fix_t"] + 600.0, magnetic_deg=12.0)]
    report = report_for(fixes + compass)
    assert report.compass_pairs == 0
    assert report.compass_offset_deg is None
    assert report.compass_error is None


def test_compass_is_not_checked_without_a_declination():
    fixes = walk_north(3)
    session = parse_lines(line(m) for m in fixes + compass_for(fixes, 0.0))
    report = analyse(session, MOVING_MPS, lambda *_: None)
    assert report.compass_pairs == 0
    assert report.declination_deg is None


def test_course_is_judged_over_a_few_metres_not_one_step():
    # 1.4 m steps are inside GPS noise; the first fixes have no 4 m baseline yet.
    report = report_for(walk_north(20))
    assert report.course_error.count == 21 - 3


def test_wrong_course_is_caught():
    fixes = [{**fix, "course_deg": 30.0} for fix in walk_north(10)]
    assert report_for(fixes).course_error.median == pytest.approx(30.0, abs=0.01)


def test_creeping_too_slowly_does_not_judge_the_course():
    # 0.5 m/s for 4 s covers 2 m: never a long enough baseline inside the window.
    assert report_for(walk_north(4, speed_mps=0.5)).course_error is None


def test_delivery_uses_the_recorded_clock_offset():
    # Server = phone + 400 ms; the fix took 100 ms to arrive.
    fixes = walk_north(3)
    lines = (line(m, recv_ms=m["fix_t"] + 500.0) for m in fixes)
    report = analyse(parse_lines(lines, clock_offset_ms=400.0), MOVING_MPS, fixed_declination)
    assert report.delivery_ms.median == pytest.approx(100.0)


def test_delivery_is_unknown_without_a_clock():
    assert report_for(walk_north(3), clock_offset_ms=None).delivery_ms is None


def test_delivery_skips_lines_without_receive_time():
    fixes = walk_north(2)
    session = parse_lines([*(line(m) for m in fixes[:2]), json.dumps(fixes[2])], 0.0)
    assert analyse(session, MOVING_MPS, fixed_declination).delivery_ms.count == 2


def test_empty_walk_has_no_numbers():
    report = analyse(parse_lines([]), MOVING_MPS, fixed_declination)
    assert report.fix_count == 0
    assert report.duration_s == 0.0
    assert report.path_m == 0.0
    assert report.straight_m == 0.0
    assert report.accuracy is None
    assert report.interval_moving is None
    assert report.reported_speed is None
    assert report.declination_deg is None


def test_long_walk_is_analysed_quickly():
    # An hour at 1 Hz with the compass at 10 Hz.
    fixes = walk_north(3600)
    compass = [
        heading_dict(t=fixes[0]["fix_t"] + i * 100.0, magnetic_deg=12.0) for i in range(36_000)
    ]
    session = parse_lines(line(m) for m in fixes + compass)
    started = time.perf_counter()
    analyse(session, MOVING_MPS, fixed_declination)
    assert time.perf_counter() - started < 2.0
