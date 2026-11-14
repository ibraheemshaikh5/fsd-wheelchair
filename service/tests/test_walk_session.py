"""Reading a recorded walk: its lines, its clock estimate, and which folder is newest."""

import json
import os
from pathlib import Path

from scripts.walk_session import latest_session, load_session, parse_lines
from walks import SESSION_NAME, START_MS, line, walk_north, write_session


def test_bad_and_other_lines_are_skipped():
    good = walk_north(2)
    lines = [
        "not json",
        json.dumps({"type": "gps", "lat": "north"}),
        json.dumps([1, 2, 3]),
        line({"v": 1, "type": "imu", "t": START_MS, "samples": []}),
        "",
        *(line(m) for m in good),
    ]
    session = parse_lines(lines)
    assert len(session.fixes) == len(good)
    assert session.skipped_lines == 3


def test_resent_fix_is_kept_once_and_lines_are_sorted():
    fixes = walk_north(3)
    resent = {**fixes[1], "t": fixes[1]["t"] + 300.0}
    session = parse_lines(line(m) for m in [fixes[2], fixes[0], fixes[1], resent, fixes[3]])
    assert [fix.fix_t for fix in session.fixes] == [f["fix_t"] for f in fixes]


def test_line_without_receive_time_still_counts():
    message = walk_north(0)[0]
    session = parse_lines([json.dumps(message)])
    assert session.fixes[0].recv_ms is None


def test_load_session_reads_lines_and_clock(tmp_path: Path):
    folder = write_session(tmp_path, SESSION_NAME, walk_north(2), clock_offset_ms=-21.5)
    session = load_session(folder)
    assert len(session.fixes) == 3
    assert session.clock_offset_ms == -21.5


def test_load_session_without_files_is_empty(tmp_path: Path):
    session = load_session(tmp_path)
    assert session.fixes == ()
    assert session.clock_offset_ms is None


def test_unreadable_or_unsynced_meta_gives_no_clock(tmp_path: Path):
    unsynced = write_session(tmp_path, "a", walk_north(1), clock_offset_ms=None)
    assert load_session(unsynced).clock_offset_ms is None
    broken = write_session(tmp_path, "b", walk_north(1))
    (broken / "meta.json").write_text("{not json")
    assert load_session(broken).clock_offset_ms is None
    odd = write_session(tmp_path, "c", walk_north(1))
    (odd / "meta.json").write_text(json.dumps({"clock": {"offset_ms": "soon"}}))
    assert load_session(odd).clock_offset_ms is None


def test_latest_session_goes_by_name_not_folder_time(tmp_path: Path):
    newer = write_session(tmp_path, "20261114T150000Z", walk_north(1))
    older = write_session(tmp_path, "20261114T140000Z", walk_north(1))
    os.utime(newer, (1, 1))
    os.utime(older, None)
    assert latest_session(tmp_path) == newer


def test_latest_session_ignores_other_folders(tmp_path: Path):
    real = write_session(tmp_path, "20261114T150000Z", walk_north(1))
    write_session(tmp_path, "zz-notes", walk_north(1))
    (tmp_path / "20261114T160000Z").mkdir()  # started, but nothing written yet
    (tmp_path / "20261114T170000Z.txt").write_text("not a folder")
    assert latest_session(tmp_path) == real


def test_latest_session_when_there_are_none(tmp_path: Path):
    assert latest_session(tmp_path) is None
    assert latest_session(tmp_path / "missing") is None
