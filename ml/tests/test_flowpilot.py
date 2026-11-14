from __future__ import annotations

import numpy as np
import pytest

from fsd_ml.drivers import Goal, Motion
from fsd_ml.flowpilot import ONNX_PATH, FlowPilotDriver, goal_slots, keep_driving, prepare


def test_frames_are_padded_resized_and_scaled() -> None:
    frames = [np.full((360, 640, 3), 255, dtype=np.uint8)] * 5

    vision = prepare(frames)

    assert vision.shape == (1, 20, 3, 216, 384)
    assert vision.dtype == np.float32
    assert vision.max() == pytest.approx(1.0)


def test_goal_is_seen_from_each_past_frame() -> None:
    standing = goal_slots((20.0, 0.0), 0.0, 0.0)
    assert standing.shape == (1, 20, 3)
    assert standing[0] == pytest.approx(np.tile([20.0, 1.0, 0.0], (20, 1)))

    # Driving straight at 1 m/s, the goal was 0.95 m further off 19 frames ago.
    moving = goal_slots((20.0, 0.0), 1.0, 0.0)
    assert moving[0, 0] == pytest.approx([20.95, 1.0, 0.0], abs=1e-4)
    assert moving[0, -1] == pytest.approx([20.0, 1.0, 0.0])

    # Turning left, a goal dead ahead now was to the left a moment ago.
    turning = goal_slots((20.0, 0.0), 1.0, 0.5)
    assert turning[0, 0, 2] > 0.3


def test_near_goals_are_pushed_out_to_keep_driving() -> None:
    assert keep_driving((6.0, 0.0)) == pytest.approx((20.0, 0.0))
    assert keep_driving((3.0, 4.0)) == pytest.approx((12.0, 16.0))
    assert keep_driving((30.0, 0.0)) == pytest.approx((30.0, 0.0))


def test_missing_weights_say_how_to_get_them(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="fsd_ml.weights flowpilot"):
        FlowPilotDriver(tmp_path / "missing.onnx")


@pytest.mark.skipif(not ONNX_PATH.is_file(), reason="FlowPilot weights not downloaded")
def test_flowpilot_proposes_six_ranked_plans() -> None:
    pytest.importorskip("onnxruntime")
    driver = FlowPilotDriver()
    frames = [np.full((216, 384, 3), 120, dtype=np.uint8)] * 20

    candidates = driver.propose(frames, Goal(point_xy=(20.0, 0.0)), Motion(1.0, 0.0))

    assert len(candidates) == 6
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)
    assert candidates[0].path_xy.shape == (80, 2)
    assert candidates[0].speed_mps is not None
