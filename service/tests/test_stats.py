import pytest

from drive_service.stats import StreamStats, record_frame, summarize

WINDOW = 5
FPS_WINDOW_S = 2.0


def _feed(stats, frames):
    for seq, mono, latency in frames:
        stats = record_frame(stats, seq=seq, received_mono=mono, latency_ms=latency, window=WINDOW)
    return stats


def test_empty_stats_summarise_to_zero():
    summary = summarize(StreamStats(), now_mono=0.0, max_age_s=FPS_WINDOW_S)
    assert summary.fps == 0.0
    assert summary.frames == 0
    assert summary.missed == 0
    assert summary.latency_p50_ms is None
    assert summary.latency_p95_ms is None


def test_record_frame_does_not_mutate_input():
    before = StreamStats()
    after = record_frame(before, seq=0, received_mono=1.0, latency_ms=10.0, window=WINDOW)
    assert before == StreamStats()
    assert after.frames == 1


def test_fps_comes_from_spacing_of_recent_frames():
    stats = _feed(StreamStats(), [(i, 10.0 + i * 0.1, None) for i in range(5)])
    assert abs(summarize(stats, now_mono=10.5, max_age_s=FPS_WINDOW_S).fps - 10.0) < 1e-6


def test_fps_goes_to_zero_when_frames_stop():
    stats = record_frame(StreamStats(), seq=0, received_mono=0.0, latency_ms=None, window=100)
    for seq in range(1, 10):
        stats = record_frame(stats, seq=seq, received_mono=seq * 0.1, latency_ms=None, window=100)
    summary = summarize(stats, now_mono=1.0, max_age_s=FPS_WINDOW_S)
    assert summary.fps == pytest.approx(10.0, abs=0.5)
    assert summarize(stats, now_mono=10.0, max_age_s=FPS_WINDOW_S).fps == 0.0


def test_fps_only_counts_frames_inside_the_window():
    old = [(i, i * 0.1, None) for i in range(5)]  # 10 fps long ago
    recent = [(5 + i, 5.0 + i * 0.5, None) for i in range(3)]  # 2 fps now
    stats = _feed(StreamStats(), [*old, *recent])
    assert summarize(stats, now_mono=6.0, max_age_s=FPS_WINDOW_S).fps == pytest.approx(2.0)


def test_single_frame_has_no_fps():
    stats = _feed(StreamStats(), [(0, 1.0, None)])
    assert summarize(stats, now_mono=1.0, max_age_s=FPS_WINDOW_S).fps == 0.0


def test_sequence_gaps_count_as_missed():
    stats = _feed(StreamStats(), [(0, 1.0, None), (1, 1.1, None), (4, 1.4, None)])
    assert summarize(stats, now_mono=2.1, max_age_s=FPS_WINDOW_S).missed == 2
    assert summarize(stats, now_mono=2.1, max_age_s=FPS_WINDOW_S).frames == 3


def test_sequence_restart_is_not_counted_as_missed():
    stats = _feed(StreamStats(), [(50, 1.0, None), (0, 2.0, None), (1, 2.1, None)])
    assert summarize(stats, now_mono=2.1, max_age_s=FPS_WINDOW_S).missed == 0


def test_window_caps_stored_samples():
    stats = _feed(StreamStats(), [(i, float(i), float(i)) for i in range(20)])
    assert len(stats.frame_times) == WINDOW
    assert len(stats.latencies_ms) == WINDOW
    assert stats.frames == 20


def test_latency_percentiles():
    stats = record_frame(StreamStats(), seq=0, received_mono=0.0, latency_ms=10.0, window=100)
    for i, latency in enumerate([20.0, 30.0, 40.0, 500.0], start=1):
        stats = record_frame(stats, seq=i, received_mono=float(i), latency_ms=latency, window=100)
    summary = summarize(stats, now_mono=4.0, max_age_s=FPS_WINDOW_S)
    assert summary.latency_p50_ms == 30.0
    assert summary.latency_p95_ms == 500.0


def test_frames_without_latency_do_not_pollute_percentiles():
    stats = _feed(StreamStats(), [(0, 1.0, None), (1, 2.0, 25.0)])
    assert summarize(stats, now_mono=2.0, max_age_s=FPS_WINDOW_S).latency_p50_ms == 25.0
