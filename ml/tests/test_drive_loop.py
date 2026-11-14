"""path_to_stick steers at a point along the path: x>0 right, y>0 forward."""

import numpy as np

from fsd_ml.chair_out import MAX_PCT
from fsd_ml.drive_loop import path_to_stick


def line(ahead: float, left: float, points: int = 10) -> np.ndarray:
    return np.linspace(0, 1, points + 1)[1:, None] * np.array([ahead, left])


def test_straight_drives_forward_without_turning():
    assert path_to_stick(line(4, 0), 25, 60, 1.0)[:2] == (0, 25)


def test_a_path_to_the_left_turns_left_and_slows():
    x, y, ahead, left, angle = path_to_stick(line(2, 2), 25, 60, 1.0)
    assert x == -MAX_PCT and 10 <= y < 25 and left > 0 and round(angle) == 45


def test_a_path_to_the_right_turns_right():
    x, y, *_ = path_to_stick(line(4, -0.4), 25, 60, 1.0)
    assert 0 < x < MAX_PCT and y > 20


def test_a_path_that_stays_put_stops():
    assert path_to_stick(line(0.1, 0.05), 25, 60, 1.0)[:2] == (0, 0)
    assert path_to_stick(np.empty((0, 2)), 25, 60, 1.0)[:2] == (0, 0)


def test_a_path_behind_turns_on_the_spot():
    x, y, *_ = path_to_stick(line(-2, 0.5), 25, 60, 1.0)
    assert x == -MAX_PCT and y == 0
