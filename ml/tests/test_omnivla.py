from __future__ import annotations

import numpy as np
import pytest

from fsd_ml.drivers import Goal
from fsd_ml.omnivla import goal_pose, implied_speed, modality_id, waypoints_to_path

PHOTO = np.zeros((4, 4, 3), dtype=np.uint8)


def test_goal_kinds_map_to_the_models_modalities() -> None:
    assert modality_id(Goal(point_xy=(5.0, 0.0))) == 4
    assert modality_id(Goal(point_xy=(5.0, 0.0), image=PHOTO)) == 5
    assert modality_id(Goal(image=PHOTO)) == 6
    assert modality_id(Goal(instruction="go to the door")) == 7
    assert modality_id(Goal(point_xy=(5.0, 0.0), instruction="go to the door")) == 8
    # A photo with an instruction is not a trained combination: the instruction wins.
    assert modality_id(Goal(instruction="go to the door", image=PHOTO)) == 7
    with pytest.raises(ValueError):
        modality_id(Goal())


def test_goal_point_is_scaled_and_pulled_in() -> None:
    assert goal_pose((5.0, -2.0)) == pytest.approx([50.0, -20.0, 1.0, 0.0])
    # 60 m away becomes 30 m in the same direction.
    assert goal_pose((60.0, 0.0)) == pytest.approx([300.0, 0.0, 1.0, 0.0])
    assert goal_pose(None) == pytest.approx([0.0, 0.0, 1.0, 0.0])


def test_waypoints_become_metres() -> None:
    waypoints = np.array([[2.0, 0.5, 1.0, 0.0], [4.0, 1.0, 1.0, 0.0]])
    assert waypoints_to_path(waypoints) == pytest.approx(np.array([[0.2, 0.05], [0.4, 0.1]]))


def test_bunched_waypoints_mean_stop() -> None:
    ahead = np.linspace(0.02, 0.16, 8)
    moving = np.column_stack([ahead, np.zeros(8)])
    assert implied_speed(moving) == pytest.approx(0.3)  # 0.10 m in a third of a second

    assert implied_speed(np.zeros((8, 2))) == 0.0
    assert implied_speed(moving * 10) == 0.5  # never more than its script allows
