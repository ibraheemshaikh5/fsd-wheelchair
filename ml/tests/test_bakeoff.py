from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FRAME_COUNT
from fsd_ml import bakeoff, weights
from fsd_ml.drivers import make_driver
from fsd_ml.recording import Recording


def test_writes_one_line_per_step(recording_dir: Path, tmp_path: Path) -> None:
    out_path = bakeoff.run(Recording(recording_dir), make_driver("straight"), tmp_path / "runs")

    assert out_path == tmp_path / "runs" / recording_dir.name / "straight.jsonl"
    lines = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert len(lines) == FRAME_COUNT
    first = lines[0]
    assert first["driver"] == "straight"
    path = first["candidates"][0]["path_xy"]
    assert path[-1] == [4.0, 0.0]
    assert all(point[1] == 0.0 for point in path)


def test_command_line_runs_and_honours_stride(
    recording_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bakeoff.main([str(recording_dir), "--out", str(tmp_path / "runs"), "--stride", "2"])

    out_path = Path(capsys.readouterr().out.strip())
    assert len(out_path.read_text().splitlines()) == FRAME_COUNT // 2


def test_unknown_names_are_rejected() -> None:
    with pytest.raises(KeyError, match="unknown driver"):
        make_driver("nope")
    with pytest.raises(KeyError, match="unknown checkpoint"):
        weights.download("nope")


def test_listing_weights_needs_no_download_library(capsys: pytest.CaptureFixture[str]) -> None:
    weights.main([])

    assert "omnivla-edge" in capsys.readouterr().out
