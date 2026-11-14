from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FRAME_COUNT, START_MS
from fsd_ml.recording import Recording, RecordingError


def test_loads_frames_in_time_order(recording_dir: Path) -> None:
    recording = Recording(recording_dir)

    assert len(recording.frames) == FRAME_COUNT
    assert [frame.n for frame in recording.frames] == list(range(1, FRAME_COUNT + 1))
    assert recording.duration_s == pytest.approx(0.25)
    assert recording.fps == pytest.approx(20.0)


def test_image_is_rgb_array(recording_dir: Path) -> None:
    recording = Recording(recording_dir)

    image = recording.image(recording.frames[0])

    assert image.shape == (36, 64, 3)
    assert image.dtype.name == "uint8"


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


def test_windows_are_consecutive_and_oldest_first(recording_dir: Path) -> None:
    recording = Recording(recording_dir)

    windows = list(recording.windows(size=3, stride=2))

    assert [[frame.n for frame in window] for window in windows] == [[1, 2, 3], [3, 4, 5]]
    with pytest.raises(ValueError):
        next(recording.windows(size=0))


def test_missing_files_are_reported(tmp_path: Path, recording_dir: Path) -> None:
    with pytest.raises(RecordingError, match="meta.json"):
        Recording(tmp_path / "nothing")

    (recording_dir / "sensors.jsonl").unlink()
    with pytest.raises(RecordingError, match="sensors.jsonl"):
        Recording(recording_dir)


def test_corrupt_line_is_reported(recording_dir: Path) -> None:
    with (recording_dir / "frames.jsonl").open("a") as frames:
        frames.write("{not json\n")

    with pytest.raises(RecordingError, match="line 7"):
        Recording(recording_dir)
