"""Rolling stream statistics: frame rate, capture-to-service latency, missed frames."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class StreamStats:
    frame_times: tuple[float, ...] = ()
    latencies_ms: tuple[float, ...] = ()
    frames: int = 0
    missed: int = 0
    last_seq: int | None = None


@dataclass(frozen=True, slots=True)
class StatsSummary:
    fps: float
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    frames: int
    missed: int


def record_frame(
    stats: StreamStats, seq: int, received_mono: float, latency_ms: float | None, window: int
) -> StreamStats:
    """Return stats with one more received frame folded in."""
    # A lower sequence number means the phone page reloaded; that is not a gap.
    gap = seq - stats.last_seq - 1 if stats.last_seq is not None and seq > stats.last_seq else 0
    latencies = (
        stats.latencies_ms if latency_ms is None else (*stats.latencies_ms, latency_ms)[-window:]
    )
    return replace(
        stats,
        frame_times=(*stats.frame_times, received_mono)[-window:],
        latencies_ms=latencies,
        frames=stats.frames + 1,
        missed=stats.missed + gap,
        last_seq=seq,
    )


def _percentile(values: tuple[float, ...], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[max(index, 0)]


def summarize(stats: StreamStats, now_mono: float, max_age_s: float) -> StatsSummary:
    """Summarise the stream as of ``now_mono``.

    The frame rate only counts frames from the last ``max_age_s`` seconds, so it
    drops to zero when the stream stops instead of showing the last known rate.
    """
    times = tuple(t for t in stats.frame_times if t >= now_mono - max_age_s)
    span = times[-1] - times[0] if len(times) > 1 else 0.0
    fps = (len(times) - 1) / span if span > 0 else 0.0
    return StatsSummary(
        fps=fps,
        latency_p50_ms=_percentile(stats.latencies_ms, 0.50),
        latency_p95_ms=_percentile(stats.latencies_ms, 0.95),
        frames=stats.frames,
        missed=stats.missed,
    )
