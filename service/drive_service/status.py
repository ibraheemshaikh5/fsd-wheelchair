"""Build the JSON status sent to the viewer and returned by /api/status."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from drive_service.clock import is_synced
from drive_service.config import Settings
from drive_service.declination import resolve_declination
from drive_service.destination import Destination, DestinationStore, destination_status
from drive_service.heading import best_heading
from drive_service.hub import Hub
from drive_service.recorder import Recorder, RecordingInfo
from drive_service.state import SensorSnapshot, age_ms, is_fresh
from drive_service.stats import StreamStats, summarize


def _frame(snapshot: SensorSnapshot, now_mono: float, fresh: bool) -> dict[str, Any] | None:
    if snapshot.frame is None:
        return None
    header = snapshot.frame.value.header
    return {
        "seq": header.seq,
        "w": header.w,
        "h": header.h,
        "age_ms": age_ms(snapshot.frame, now_mono),
        "fresh": fresh,
    }


def _gps(snapshot: SensorSnapshot, now_mono: float, fresh: bool) -> dict[str, Any] | None:
    if snapshot.gps is None:
        return None
    fix = snapshot.gps.value
    return {
        "lat": fix.lat,
        "lon": fix.lon,
        "accuracy_m": fix.accuracy_m,
        "altitude_m": fix.altitude_m,
        "speed_mps": fix.speed_mps,
        "course_deg": fix.course_deg,
        "age_ms": age_ms(snapshot.gps, now_mono),
        "fresh": fresh,
    }


def _heading(
    snapshot: SensorSnapshot,
    now_mono: float,
    gps_fresh: bool,
    compass_fresh: bool,
    settings: Settings,
) -> dict[str, Any] | None:
    fresh_fix = snapshot.gps.value if snapshot.gps and gps_fresh else None
    declination_deg = resolve_declination(settings.magnetic_declination_deg, fresh_fix)
    best = best_heading(
        fresh_fix,
        snapshot.heading.value if snapshot.heading and compass_fresh else None,
        settings.moving_speed_mps,
        declination_deg,
    )
    if best is None and snapshot.heading is None:
        return None
    compass = snapshot.heading.value if snapshot.heading else None
    return {
        # In ``reference`` north; compass_deg below stays raw magnetic.
        "deg": best.deg if best else None,
        "source": best.source if best else None,
        "reference": best.reference if best else None,
        "declination_deg": declination_deg,
        "compass_deg": compass.magnetic_deg if compass else None,
        "compass_accuracy_deg": compass.accuracy_deg if compass else None,
        "compass_age_ms": age_ms(snapshot.heading, now_mono),
        "compass_fresh": compass_fresh,
    }


def build_status(
    snapshot: SensorSnapshot,
    stats: StreamStats,
    recording: RecordingInfo,
    rejected: int,
    rate_limited: int,
    now_mono: float,
    settings: Settings,
    destination: Destination | None = None,
) -> dict[str, Any]:
    frame_fresh = is_fresh(snapshot.frame, settings.frame_stale_ms, now_mono)
    gps_fresh = is_fresh(snapshot.gps, settings.gps_stale_ms, now_mono)
    compass_fresh = is_fresh(snapshot.heading, settings.heading_stale_ms, now_mono)
    # Only the connected phone's clock counts; a departed phone's estimate lingers
    # until the next one attaches.
    clock_synced = snapshot.phone_connected and is_synced(snapshot.clock, settings.max_clock_rtt_ms)
    hello = snapshot.hello
    return {
        "type": "status",
        "phone_connected": snapshot.phone_connected,
        "clock_synced": clock_synced,
        # Outdoor definition: a live camera and a live GPS fix, judged by capture time.
        "sensors_ok": snapshot.phone_connected and clock_synced and frame_fresh and gps_fresh,
        "frame": _frame(snapshot, now_mono, frame_fresh),
        "gps": _gps(snapshot, now_mono, gps_fresh),
        "heading": _heading(snapshot, now_mono, gps_fresh, compass_fresh, settings),
        "clock": asdict(snapshot.clock) if snapshot.clock else None,
        "stats": {
            **asdict(summarize(stats, now_mono, settings.fps_window_s)),
            "rejected": rejected,
            "rate_limited": rate_limited,
        },
        "camera": hello.camera.model_dump() if hello else None,
        "mount": hello.mount.model_dump() if hello and hello.mount else None,
        "recording": asdict(recording),
        "destination": destination_status(
            destination, snapshot.gps.value if snapshot.gps and gps_fresh else None
        ),
    }


def live_status(
    hub: Hub,
    recorder: Recorder,
    destinations: DestinationStore,
    now_mono: float,
    settings: Settings,
) -> dict[str, Any]:
    """The status right now, as served by GET /api/status and every /ws/viewer message."""
    return build_status(
        hub.snapshot,
        hub.stats,
        recorder.info,
        hub.rejected,
        hub.rate_limited,
        now_mono,
        settings,
        destination=destinations.current,
    )
