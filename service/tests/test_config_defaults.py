"""Defaults that the phone's frame rate and the viewers depend on."""

from drive_service.config import Settings

PHONE_TOP_FPS = 60
TWO_HOURS_S = 2 * 60 * 60
IMU_FLUSH_MS = 50  # web/capture/session.ts
HEADING_PER_S = 10
MIN_DISK_STALL_S = 8.0


def test_viewers_get_thirty_updates_a_second(tmp_path):
    assert Settings(record_dir=tmp_path).viewer_fps == 30.0


def test_recording_frame_limit_holds_two_hours_at_the_phone_top_rate(tmp_path):
    assert Settings(record_dir=tmp_path).recorder_max_frames == TWO_HOURS_S * PHONE_TOP_FPS


def test_motion_goes_stale_after_250_ms(tmp_path):
    assert Settings(record_dir=tmp_path).motion_stale_ms == 250.0


def test_recorder_queue_absorbs_seconds_of_disk_stall_at_the_phone_top_rate(tmp_path):
    # Every frame plus each IMU batch, its motion line, and heading readings.
    imu_batches_per_s = 1000 / IMU_FLUSH_MS
    items_per_s = PHONE_TOP_FPS + 2 * imu_batches_per_s + HEADING_PER_S
    queue = Settings(record_dir=tmp_path).recorder_queue_size
    assert queue / items_per_s >= MIN_DISK_STALL_S
