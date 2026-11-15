"""The bench's settings, journal and notes."""

import json

import pytest

from fsd_ml.bench import CASES, DEFAULTS, Bench, case


def test_settings_are_the_defaults_until_changed_and_changes_are_remembered(tmp_path):
    bench = Bench(tmp_path)
    assert bench.settings() == DEFAULTS
    bench.set(drive_stick_pct=4, turn_stick_pct=2)
    assert Bench(tmp_path).settings()["drive_stick_pct"] == 4.0
    with pytest.raises(KeyError):
        bench.set(warp_speed=9)
    # A hand-edited file with junk in it does not break a run.
    (tmp_path / "settings.json").write_text('{"drive_stick_pct": "fast"}')
    assert bench.settings() == DEFAULTS


def test_a_run_its_result_and_the_feedback_come_back_together(tmp_path):
    bench = Bench(tmp_path)
    run = bench.new_run("straight", {"distance_m": 0.76})
    assert bench.latest()["straight"].get("result") is None
    bench.write("result", run=run, outcome="finished", reason="drove", seconds=5.1)
    bench.write("feedback", run=run, rating="close", measured="26 in", text="veered left")
    latest = bench.latest()["straight"]
    assert latest["result"]["outcome"] == "finished"
    assert latest["feedback"]["measured"] == "26 in"
    assert latest["run"] == run


def test_notes_are_read_from_the_file_claude_writes(tmp_path):
    bench = Bench(tmp_path)
    assert bench.notes()["summary"] is None
    (tmp_path / "notes.json").write_text(json.dumps({"summary": "softened turns", "cases": {}}))
    assert bench.notes()["summary"] == "softened turns"


def test_every_case_can_be_looked_up_and_has_what_the_page_needs():
    for entry in CASES:
        assert case(entry["id"]) is entry
        assert entry["title"] and entry["does"] and entry["ask"] and entry["kind"]
    assert case("nope") is None
