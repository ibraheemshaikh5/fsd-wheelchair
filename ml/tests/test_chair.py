from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from fsd_ml import chair
from fsd_ml.drivers import FanDriver
from fsd_ml.pipeline import ChairStatus, Pipeline, PipelineConfig
from test_pipeline import route_from, sensors


def test_measurements_are_saved_in_metres(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    file = tmp_path / "chair.json"
    chair.main(
        ["--height", "32", "--offset", "27", "--side", "right", "--from-rear", "95", "--tilt", "3",
         "--wall-width", "200", "--chair-width", "62", "--chair-length", "105", "--file", str(file)]
    )  # fmt: skip

    setup = chair.load(file)
    assert setup.measured is True
    assert (setup.camera.height_m, setup.camera.left_m) == (0.32, -0.27)  # right is negative
    assert setup.camera.hfov_deg == pytest.approx(90.0)  # 2 m of wall from 1 m away
    assert (setup.chair.width_m, setup.chair.length_m) == (0.62, 1.05)
    # 95 cm from the back of a 105 cm chair is 42.5 cm ahead of its middle.
    assert setup.lens_xy == pytest.approx((0.425, -0.27))
    assert "0.27 m right of centre" in capsys.readouterr().out


def test_one_number_can_be_changed_without_the_rest(tmp_path: Path) -> None:
    file = tmp_path / "chair.json"
    chair.main(["--height", "32", "--offset", "27", "--side", "left", "--file", str(file)])
    chair.main(["--tilt", "8", "--file", str(file)])
    chair.main(["--side", "right", "--file", str(file)])

    setup = chair.load(file)
    assert (setup.camera.height_m, setup.camera.tilt_down_deg) == (0.32, 8.0)
    assert setup.camera.left_m == -0.27


def test_reading_without_numbers_changes_nothing(tmp_path: Path, capsys) -> None:
    file = tmp_path / "chair.json"
    chair.main(["--file", str(file)])
    assert not file.exists()
    assert "placeholders" in capsys.readouterr().out
    assert chair.load(file).measured is False


def test_wrong_units_are_questioned(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    file = tmp_path / "chair.json"
    # Metres typed where centimetres were asked for.
    chair.main(["--height", "0.32", "--chair-width", "0.62", "--file", str(file)])
    out = capsys.readouterr().out
    assert "check this: a lens 0.00 m off the floor" in out
    assert "check this: a chair 0.01 m by" in out


def test_paths_are_shifted_from_the_lens_to_the_chair() -> None:
    setup = chair.ChairSetup(
        chair.Camera(left_m=-0.3, from_rear_m=1.0), chair.Body(length_m=1.2), measured=True
    )
    # Straight ahead of a lens on the front right corner is right of the chair's middle.
    shifted = setup.to_chair(np.array([[1.0, 0.0], [2.0, 0.0]]))
    assert shifted == pytest.approx(np.array([[1.4, -0.3], [2.4, -0.3]]))

    pipeline = Pipeline(FanDriver(), PipelineConfig(lens_xy=setup.lens_xy))
    pipeline.set_route(route_from([(0, 0), (0, 30)]))
    decision = pipeline.step(sensors(1000.0), ChairStatus(), 0.05)
    plain = Pipeline(FanDriver())
    plain.set_route(route_from([(0, 0), (0, 30)]))
    unshifted = plain.step(sensors(1000.0), ChairStatus(), 0.05)
    assert decision.path_xy - unshifted.path_xy == pytest.approx(
        np.tile([0.4, -0.3], (len(decision.path_xy), 1))
    )


def test_the_settings_file_in_the_repo_loads() -> None:
    assert chair.check(chair.load()) == []
