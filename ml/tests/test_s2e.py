from __future__ import annotations

import numpy as np
import pytest

from fsd_ml.s2e import ONNX_PATH, S2EDriver, goal_input, prepare


def test_inputs_have_the_shapes_the_model_takes() -> None:
    frames = [np.full((216, 384, 3), 255, dtype=np.uint8)] * 11

    images = prepare(frames)

    assert images.shape == (1, 11, 3, 256, 256)
    assert images.dtype == np.float32
    assert images.max() == pytest.approx(1.0)
    # 10 m dead ahead: distance / 200, cos 0, sin 0.
    assert goal_input((10.0, 0.0))[0] == pytest.approx([0.05, 1.0, 0.0])
    # A goal to the left has a positive sine.
    assert goal_input((0.0, 5.0))[0][2] == pytest.approx(1.0)


def test_missing_weights_say_how_to_get_them(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="fsd_ml.weights s2e"):
        S2EDriver(tmp_path / "s2e.onnx")


@pytest.mark.skipif(not ONNX_PATH.is_file(), reason="S2E weights not downloaded")
def test_s2e_steers_toward_the_goal() -> None:
    pytest.importorskip("onnxruntime")
    driver = S2EDriver()
    frames = [np.full((216, 384, 3), 120, dtype=np.uint8)] * driver.context_frames
    from fsd_ml.drivers import Goal, Motion

    ahead = driver.propose(frames, Goal(point_xy=(5.0, 0.0)), Motion())[0].path_xy
    left = driver.propose(frames, Goal(point_xy=(5.0, 3.0)), Motion())[0].path_xy

    assert ahead.shape == (10, 2)
    assert ahead[-1, 0] > 1.0
    assert left[-1, 1] > ahead[-1, 1] + 0.5
