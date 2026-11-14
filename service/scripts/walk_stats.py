"""Speed, location and heading numbers from a recorded walk.

Pure functions over a loaded ``Session`` (see ``walk_session.py``), so the
numbers can be checked against synthetic walks. ``gps_report.py`` prints them.

Only steps where the phone was moving count towards distance, moving time and
the speed check: at rest iOS reports speed 0 while the position still wanders
by metres, which would otherwise add phantom distance.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from drive_service.declination import wmm_declination_deg
from drive_service.geo import bearing_deg, distance_m
from scripts.walk_session import CompassReading, Fix, Session

MS_PER_S = 1000.0
HALF_TURN_DEG = 180.0
FULL_TURN_DEG = 360.0
# Longest step between two moving fixes (iOS reports about once a second while moving).
MAX_STEP_S = 3.0
# Longer than this between fixes is a gap in the stream, not a slow fix rate
# (an iPhone at rest reports about every 5 s).
GAP_S = 12.0
# Fixes this coarse are left out of distance; they still count everywhere else.
MAX_DISTANCE_ACCURACY_M = 30.0
# The phone's course is checked against the bearing from an earlier fix at least
# this far back (closer is mostly GPS noise), and no more than the window ago.
MIN_COURSE_DISTANCE_M = 4.0
MAX_COURSE_WINDOW_S = 6.0
# A compass reading is paired with a fix only if taken this close to it.
MAX_COMPASS_GAP_MS = 500.0
P90 = 0.9

DeclinationFn = Callable[[float, float, float | None, datetime], float | None]


@dataclass(frozen=True, slots=True)
class Spread:
    count: int
    median: float
    p90: float
    max: float


@dataclass(frozen=True, slots=True)
class WalkReport:
    fix_count: int
    duration_s: float
    path_m: float
    straight_m: float
    moving_s: float
    gap_count: int
    gap_s: float
    interval_moving: Spread | None
    interval_rest: Spread | None
    accuracy: Spread | None
    reported_speed: Spread | None
    speed_error: Spread | None
    speed_bias_mps: float | None
    course_error: Spread | None
    compass_pairs: int
    compass_offset_deg: float | None
    compass_error: Spread | None
    declination_deg: float | None
    delivery_ms: Spread | None
    skipped_lines: int


@dataclass(frozen=True, slots=True)
class _Step:
    start: Fix
    end: Fix
    dt_s: float
    distance_m: float


def angle_diff_deg(a: float, b: float) -> float:
    """a minus b, wrapped to [-180, 180)."""
    return (a - b + HALF_TURN_DEG) % FULL_TURN_DEG - HALF_TURN_DEG


def circular_mean_deg(angles: Sequence[float]) -> float | None:
    """Mean direction of signed angles, in (-180, 180]; plain medians break at ±180."""
    if not angles:
        return None
    sin_sum = sum(math.sin(math.radians(a)) for a in angles)
    cos_sum = sum(math.cos(math.radians(a)) for a in angles)
    return math.degrees(math.atan2(sin_sum, cos_sum))


def spread(values: Iterable[float]) -> Spread | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(0, math.ceil(P90 * len(ordered)) - 1)
    return Spread(len(ordered), statistics.median(ordered), ordered[rank], ordered[-1])


def _is_moving(fix: Fix, moving_speed_mps: float) -> bool:
    return fix.speed_mps is not None and fix.speed_mps >= moving_speed_mps


def _steps(fixes: tuple[Fix, ...]) -> list[_Step]:
    return [
        _Step(a, b, (b.fix_t - a.fix_t) / MS_PER_S, distance_m(a.lat, a.lon, b.lat, b.lon))
        for a, b in zip(fixes, fixes[1:], strict=False)
    ]


def _walking_steps(steps: list[_Step], moving_speed_mps: float) -> list[_Step]:
    """Steps where the phone was moving at both ends, without a gap in between."""
    return [
        step
        for step in steps
        if 0 < step.dt_s <= MAX_STEP_S
        and _is_moving(step.start, moving_speed_mps)
        and _is_moving(step.end, moving_speed_mps)
    ]


def _speed_differences(walking: list[_Step]) -> list[float]:
    """Phone-reported speed minus the speed implied by the positions, per step."""
    return [
        (step.start.speed_mps + step.end.speed_mps) / 2 - step.distance_m / step.dt_s
        for step in walking
        if step.start.speed_mps is not None and step.end.speed_mps is not None
    ]


def _usable_for_distance(fix: Fix) -> bool:
    return fix.accuracy_m <= MAX_DISTANCE_ACCURACY_M


def _straight_m(fixes: tuple[Fix, ...]) -> float:
    usable = [fix for fix in fixes if _usable_for_distance(fix)]
    if len(usable) < 2:
        return 0.0
    first, last = usable[0], usable[-1]
    return distance_m(first.lat, first.lon, last.lat, last.lon)


def _track_bearing(fixes: tuple[Fix, ...], index: int) -> float | None:
    """Bearing to fix ``index`` from the latest earlier fix far enough back, if recent."""
    end = fixes[index]
    for back in range(index - 1, -1, -1):
        start = fixes[back]
        if (end.fix_t - start.fix_t) / MS_PER_S > MAX_COURSE_WINDOW_S:
            return None
        if distance_m(start.lat, start.lon, end.lat, end.lon) >= MIN_COURSE_DISTANCE_M:
            return bearing_deg(start.lat, start.lon, end.lat, end.lon)
    return None


def _course_errors(fixes: tuple[Fix, ...], moving_speed_mps: float) -> list[float]:
    """How far the phone's course is from the direction the positions moved."""
    errors = []
    for index, fix in enumerate(fixes):
        if fix.course_deg is None or not _is_moving(fix, moving_speed_mps):
            continue
        bearing = _track_bearing(fixes, index)
        if bearing is not None:
            errors.append(abs(angle_diff_deg(fix.course_deg, bearing)))
    return errors


def _nearest_compass(
    compass: tuple[CompassReading, ...], times: list[float], t: float
) -> CompassReading | None:
    at = bisect.bisect_left(times, t)
    nearby = [compass[i] for i in (at - 1, at) if 0 <= i < len(compass)]
    best = min(nearby, key=lambda reading: abs(reading.t - t), default=None)
    if best is None or abs(best.t - t) > MAX_COMPASS_GAP_MS:
        return None
    return best


def _compass_offsets(
    session: Session, moving_speed_mps: float, declination_deg: float
) -> list[float]:
    """True compass heading minus GPS course, for each moving fix with a nearby reading.

    Both times are on the phone's clock (compass ``t`` and the fix's ``fix_t``).
    """
    times = [reading.t for reading in session.compass]
    offsets = []
    for fix in session.fixes:
        if fix.course_deg is None or not _is_moving(fix, moving_speed_mps):
            continue
        reading = _nearest_compass(session.compass, times, fix.fix_t)
        if reading is not None:
            true_deg = (reading.magnetic_deg + declination_deg) % FULL_TURN_DEG
            offsets.append(angle_diff_deg(true_deg, fix.course_deg))
    return offsets


def _declination(session: Session, declination: DeclinationFn) -> float | None:
    if not session.fixes:
        return None
    first = session.fixes[0]
    when = datetime.fromtimestamp(first.fix_t / MS_PER_S, tz=UTC)
    return declination(first.lat, first.lon, first.altitude_m, when)


def _delivery_ms(session: Session) -> Spread | None:
    """Fix time to arrival at the service, on one clock; unknown without a clock offset."""
    offset = session.clock_offset_ms
    if offset is None:
        return None
    return spread(
        fix.recv_ms - (fix.fix_t + offset) for fix in session.fixes if fix.recv_ms is not None
    )


def analyse(
    session: Session,
    moving_speed_mps: float,
    declination: DeclinationFn = wmm_declination_deg,
) -> WalkReport:
    fixes = session.fixes
    steps = _steps(fixes)
    walking = _walking_steps(steps, moving_speed_mps)
    gaps = [step for step in steps if step.dt_s > GAP_S]
    in_stream = [step for step in steps if step.dt_s <= GAP_S]
    speed_diffs = _speed_differences(walking)
    declination_deg = _declination(session, declination)
    offsets = (
        []
        if declination_deg is None
        else _compass_offsets(session, moving_speed_mps, declination_deg)
    )
    return WalkReport(
        fix_count=len(fixes),
        duration_s=(fixes[-1].fix_t - fixes[0].fix_t) / MS_PER_S if fixes else 0.0,
        path_m=sum(
            step.distance_m
            for step in walking
            if _usable_for_distance(step.start) and _usable_for_distance(step.end)
        ),
        straight_m=_straight_m(fixes),
        moving_s=sum(step.dt_s for step in walking),
        gap_count=len(gaps),
        gap_s=sum(step.dt_s for step in gaps),
        interval_moving=spread(s.dt_s for s in in_stream if _is_moving(s.end, moving_speed_mps)),
        interval_rest=spread(s.dt_s for s in in_stream if not _is_moving(s.end, moving_speed_mps)),
        accuracy=spread(fix.accuracy_m for fix in fixes),
        reported_speed=spread(
            fix.speed_mps
            for fix in fixes
            if fix.speed_mps is not None and _is_moving(fix, moving_speed_mps)
        ),
        speed_error=spread(abs(diff) for diff in speed_diffs),
        speed_bias_mps=statistics.median(speed_diffs) if speed_diffs else None,
        course_error=spread(_course_errors(fixes, moving_speed_mps)),
        compass_pairs=len(offsets),
        compass_offset_deg=circular_mean_deg(offsets),
        compass_error=spread(abs(offset) for offset in offsets),
        declination_deg=declination_deg,
        delivery_ms=_delivery_ms(session),
        skipped_lines=session.skipped_lines,
    )
