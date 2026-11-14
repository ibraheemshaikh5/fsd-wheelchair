"""Print speed, location and heading numbers for a recorded walk.

    uv run python -m scripts.gps_report              # the newest recording
    uv run python -m scripts.gps_report recordings/20261114T150000Z

Record a walk from the sensor viewer first (Start recording, walk, Stop).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

from drive_service.config import Settings
from drive_service.declination import wmm_declination_deg
from scripts.walk_session import latest_session, load_session
from scripts.walk_stats import DeclinationFn, Spread, WalkReport, analyse

MPH_PER_MPS = 2.236_936
SECONDS_PER_MINUTE = 60


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), SECONDS_PER_MINUTE)
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _spread(spread: Spread | None, unit: str, digits: int = 1, missing: str = "none") -> str:
    if spread is None:
        return missing
    return (
        f"median {spread.median:.{digits}f}{unit}, p90 {spread.p90:.{digits}f}{unit}, "
        f"worst {spread.max:.{digits}f}{unit} ({spread.count})"
    )


def _speed_line(report: WalkReport) -> str:
    speed = report.reported_speed
    if speed is None:
        return "no moving fixes with speed"
    return (
        f"median {speed.median:.2f} m/s ({speed.median * MPH_PER_MPS:.1f} mph), "
        f"top {speed.max:.2f} m/s ({speed.max * MPH_PER_MPS:.1f} mph), from {speed.count} fixes"
    )


def _speed_check_line(report: WalkReport) -> str:
    if report.speed_error is None or report.speed_bias_mps is None:
        return "not enough moving fixes"
    return (
        f"phone speed minus speed from positions: median {report.speed_bias_mps:+.2f} m/s, "
        f"size {_spread(report.speed_error, ' m/s', digits=2)}"
    )


def _compass_line(report: WalkReport) -> str:
    if report.declination_deg is None:
        return "no declination for this place, not checked"
    if report.compass_offset_deg is None:
        return "not enough movement to judge"
    return (
        f"compass minus GPS course: average {report.compass_offset_deg:+.0f}°, "
        f"size {_spread(report.compass_error, '°', digits=0)}, "
        f"declination {report.declination_deg:+.1f}°"
    )


def _delivery_line(report: WalkReport) -> str:
    if report.delivery_ms is None:
        return "unknown: clocks were not synced when recording started"
    return f"fix to service: {_spread(report.delivery_ms, ' ms', digits=0)}"


def format_report(name: str, report: WalkReport) -> str:
    if report.fix_count == 0:
        return f"Walk {name}\n  no GPS fixes in this recording\n"
    lines = [
        f"Walk {name}",
        f"  GPS           {report.fix_count} fixes over {_duration(report.duration_s)}",
        f"  Fix interval  moving: {_spread(report.interval_moving, ' s')}",
        f"                at rest: {_spread(report.interval_rest, ' s')}",
        f"  Accuracy      {_spread(report.accuracy, ' m')}",
        f"  Distance      path {report.path_m:.0f} m while moving, "
        f"start to end {report.straight_m:.0f} m, moving for {_duration(report.moving_s)}",
        f"  Speed         {_speed_line(report)}",
        f"  Speed check   {_speed_check_line(report)}",
        "  Course check  phone course vs direction moved: "
        + _spread(report.course_error, "°", digits=0, missing="not enough movement to judge"),
        f"  Compass       {_compass_line(report)}",
        f"  Delivery      {_delivery_line(report)}",
    ]
    if report.gap_count:
        lines.append(
            f"  Gaps          {_plural(report.gap_count, 'gap')} in the stream, "
            f"{_duration(report.gap_s)} in all"
        )
    if report.skipped_lines:
        lines.append(f"  Skipped       {_plural(report.skipped_lines, 'unreadable line')}")
    return "\n".join(lines) + "\n"


def declination_source(configured_deg: float | None) -> DeclinationFn:
    """The configured declination if set (as the live service does), else the WMM model."""
    if configured_deg is None:
        return wmm_declination_deg
    return lambda *_: configured_deg


def run(
    session_dir: Path | None,
    record_dir: Path,
    moving_speed_mps: float,
    configured_declination_deg: float | None,
    out: TextIO,
) -> int:
    """Write the report for one session; 0 on success, 1 if there was nothing to report."""
    chosen = session_dir if session_dir is not None else latest_session(record_dir)
    if chosen is None:
        out.write(f"no recordings in {record_dir}\n")
        return 1
    if not chosen.is_dir():
        out.write(f"{chosen} is not a recording folder\n")
        return 1
    declination = declination_source(configured_declination_deg)
    report = analyse(load_session(chosen), moving_speed_mps, declination)
    out.write(format_report(chosen.name, report))
    return 0 if report.fix_count else 1


def main() -> None:  # pragma: no cover - thin command-line wrapper
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session", nargs="?", type=Path, help="recording folder (default: newest)")
    args = parser.parse_args()
    settings = Settings()
    code = run(
        args.session,
        settings.record_dir,
        settings.moving_speed_mps,
        settings.magnetic_declination_deg,
        sys.stdout,
    )
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
