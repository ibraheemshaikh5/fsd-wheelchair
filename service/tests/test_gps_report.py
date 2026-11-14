"""The walk report command: picking the session and printing readable numbers."""

import io
from pathlib import Path

import pytest

from helpers import heading_dict
from scripts.gps_report import declination_source, format_report, run
from scripts.walk_session import parse_lines
from scripts.walk_stats import analyse
from walks import (
    MOVING_MPS,
    SESSION_NAME,
    at_rest,
    fixed_declination,
    line,
    shifted,
    walk_north,
    write_session,
)


def _report(messages: list[dict], clock_offset_ms: float | None = 0.0):
    lines = (line(m, recv_ms=m["t"] + 500.0) for m in messages)
    return analyse(parse_lines(lines, clock_offset_ms), MOVING_MPS, fixed_declination)


def test_report_shows_speed_distance_and_accuracy():
    fixes = walk_north(20)
    compass = [heading_dict(t=fix["fix_t"], magnetic_deg=12.0) for fix in fixes]
    text = format_report(SESSION_NAME, _report(fixes + compass))

    assert SESSION_NAME in text
    assert "21 fixes over 20 s" in text
    assert "path 28 m" in text
    assert "median 1.40 m/s (3.1 mph)" in text
    assert "median 4.5 m" in text
    assert "compass minus GPS course: average +0°" in text
    assert "fix to service: median 500 ms" in text
    assert "Gaps" not in text


def test_report_says_what_it_could_not_measure():
    text = format_report("rest", _report(at_rest(20), clock_offset_ms=None))
    assert "moving: none" in text
    assert "no moving fixes with speed" in text
    assert "not enough movement" in text
    assert "clocks were not synced" in text


def test_report_shows_gaps_and_skipped_lines():
    messages = walk_north(10) + shifted(walk_north(10), 70.0)
    lines = [*(line(m) for m in messages), "garbage"]
    report = analyse(parse_lines(lines, 0.0), MOVING_MPS, fixed_declination)
    text = format_report("gappy", report)
    assert "1 gap in the stream, 1 min 0 s in all" in text
    assert "1 unreadable line" in text


def test_report_without_declination():
    report = analyse(parse_lines(line(m) for m in walk_north(5)), MOVING_MPS, lambda *_: None)
    assert "no declination for this place" in format_report("x", report)


def test_empty_report():
    text = format_report("empty", analyse(parse_lines([]), MOVING_MPS, fixed_declination))
    assert "no GPS fixes" in text


def test_configured_declination_wins_over_the_model():
    assert declination_source(-11.0)(38.98, -76.94, None, None) == -11.0


def test_without_configured_declination_the_model_is_used():
    from datetime import UTC, datetime

    model = declination_source(None)(38.98, -76.94, 40.0, datetime(2026, 11, 14, tzinfo=UTC))
    assert model == pytest.approx(-10.8, abs=1.0)


def test_run_prints_the_newest_report(tmp_path: Path):
    write_session(tmp_path, SESSION_NAME, walk_north(10))
    out = io.StringIO()
    assert run(None, tmp_path, MOVING_MPS, None, out) == 0
    assert SESSION_NAME in out.getvalue()


def test_run_uses_the_configured_declination(tmp_path: Path):
    fixes = walk_north(10)
    compass = [heading_dict(t=f["fix_t"], magnetic_deg=11.0) for f in fixes]
    write_session(tmp_path, SESSION_NAME, fixes + compass)
    out = io.StringIO()
    run(None, tmp_path, MOVING_MPS, -11.0, out)
    assert "average +0°" in out.getvalue()
    assert "declination -11.0°" in out.getvalue()


def test_run_fails_without_a_session(tmp_path: Path):
    out = io.StringIO()
    assert run(None, tmp_path, MOVING_MPS, None, out) == 1
    assert "no recordings" in out.getvalue()
    assert run(tmp_path / "nope", tmp_path, MOVING_MPS, None, out) == 1
    assert "not a recording folder" in out.getvalue()


def test_run_fails_on_a_session_without_gps(tmp_path: Path):
    session = write_session(tmp_path, "s", [heading_dict()])
    out = io.StringIO()
    assert run(session, tmp_path, MOVING_MPS, None, out) == 1
    assert "no GPS fixes" in out.getvalue()
