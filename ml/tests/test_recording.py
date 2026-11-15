from __future__ import annotations

from pathlib import Path

import pytest

from conftest import START_MS
from fsd_ml.recording import Recording


def test_gps_is_never_from_the_future(recording_dir: Path) -> None:
    recording = Recording(recording_dir)

    # The fix is timed by fix_t (START+10), not the later message time.
    assert recording.gps_at(START_MS + 5) is None
    fix = recording.gps_at(START_MS + 10)
    assert fix is not None
    assert fix.speed_mps == pytest.approx(1.1)
    assert recording.heading_at(START_MS + 100) is None
    assert recording.heading_at(START_MS + 500) is not None


def test_imu_samples_get_absolute_times(recording_dir: Path) -> None:
    recording = Recording(recording_dir)

    assert recording.imu.shape == (2, 7)
    assert recording.imu[:, 0].tolist() == [START_MS + 100, START_MS + 110]
    assert recording.imu_between(START_MS + 100, START_MS + 110).shape == (1, 7)
