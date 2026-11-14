from __future__ import annotations

import json
from pathlib import Path

import pytest

from fsd_ml.sim.run import main, simulate
from fsd_ml.sim.scenarios import SCENARIOS


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_ends_as_expected_without_a_collision(name: str) -> None:
    scenario = SCENARIOS[name]

    result = simulate(scenario)

    assert result.arrived == scenario.expect_arrival
    assert not result.collided


def events(name: str) -> str:
    return "\n".join(event for _, event in simulate(SCENARIOS[name]).events)


def test_pedestrian_is_passed_with_room() -> None:
    result = simulate(SCENARIOS["pedestrian"])
    assert result.min_clearance_m is not None
    assert result.min_clearance_m > 0.15


def test_stops_explain_themselves() -> None:
    assert "TAKE OVER: Path blocked" in events("stuck")
    assert "Waiting for pedestrian" in events("crossing")
    assert "TAKE OVER: Path blocked" not in events("crossing")
    assert "Camera feed lost" in events("dropout")
    assert "TAKE OVER: Emergency stop pressed" in events("estop")
    assert "TAKE OVER: Driver model not responding" in events("dead-model")
    assert "manual" in events("joystick")


def test_run_writes_the_three_outputs_and_a_picture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["clear", "--out", str(tmp_path)])

    folder = tmp_path / "clear"
    assert "arrived" in capsys.readouterr().out
    assert (folder / "track.png").stat().st_size > 0
    lines = {
        name: (folder / f"{name}.jsonl").read_text().splitlines()
        for name in ("decisions", "commands", "drive_state")
    }
    assert len(lines["decisions"]) == len(lines["commands"]) == len(lines["drive_state"]) > 100
    assert json.loads(lines["commands"][-1])["v_mps"] == 0.0
    assert json.loads(lines["drive_state"][-1])["mode"] == "arrived"
    assert json.loads(lines["decisions"][5])["model"]["name"] == "fan"
