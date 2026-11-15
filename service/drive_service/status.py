"""Build the JSON status sent to the viewer and returned by /api/status."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from drive_service.chair import chair_status
from drive_service.clock import is_synced
from drive_service.config import (
    FPS_WINDOW_S,
    FRAME_STALE_MS,
    GPS_STALE_MS,
    HEADING_STALE_MS,
    LINK_STALE_MS,
    MAX_CLOCK_RTT_MS,
    MOTION_STALE_MS,
    MOVING_SPEED_MPS,
)
from drive_service.declination import resolve_declination
from drive_service.destination import (
    Destination,
    DestinationStore,
    Goal,
    destination_status,
    goal_status,
)
from drive_service.heading import best_heading
from drive_service.hub import Hub
from drive_service.recorder import Recorder, RecordingInfo
from drive_service.state import MS_PER_S, SensorSnapshot, age_ms, is_fresh
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


def _motion(snapshot: SensorSnapshot, now_mono: float) -> dict[str, Any] | None:
    """Speed, turn rate (positive = left) and acceleration, judged by the newest IMU sample."""
    if snapshot.motion is None:
        return None
    estimate = snapshot.motion.value
    return {
        "speed_mps": estimate.speed_mps,
        "speed_source": estimate.speed_source,
        "yaw_rate_dps": estimate.yaw_rate_dps,
        "accel_fwd_mps2": estimate.accel_fwd_mps2,
        "accel_lat_mps2": estimate.accel_lat_mps2,
        "age_ms": age_ms(snapshot.motion, now_mono),
        "fresh": is_fresh(snapshot.motion, MOTION_STALE_MS, now_mono),
    }


# Wire fields of the link message that the status leaves out.
_LINK_ENVELOPE = frozenset({"v", "type", "t"})


def _link(snapshot: SensorSnapshot, now_mono: float) -> dict[str, Any] | None:
    """The phone's latest uplink report, aged by when the service received it."""
    if snapshot.link is None:
        return None
    age = (now_mono - snapshot.link.received_mono) * MS_PER_S
    return {
        **snapshot.link.value.model_dump(exclude=_LINK_ENVELOPE),
        "age_ms": age,
        "fresh": age <= LINK_STALE_MS,
    }


def _heading(
    snapshot: SensorSnapshot, now_mono: float, gps_fresh: bool, compass_fresh: bool
) -> dict[str, Any] | None:
    fresh_fix = snapshot.gps.value if snapshot.gps and gps_fresh else None
    declination_deg = resolve_declination(fresh_fix)
    best = best_heading(
        fresh_fix,
        snapshot.heading.value if snapshot.heading and compass_fresh else None,
        MOVING_SPEED_MPS,
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


def _frame_source(
    snapshot: SensorSnapshot, phone_clock_synced: bool, external_camera: bool
) -> dict[str, Any]:
    """Where frames come from, and whether that sender is connected with a synced clock."""
    if not external_camera:
        return {
            "kind": "phone",
            "connected": snapshot.phone_connected,
            "clock_synced": phone_clock_synced,
        }
    connected = snapshot.camera_connected
    return {
        "kind": "external",
        "connected": connected,
        "clock_synced": connected and is_synced(snapshot.camera_clock, MAX_CLOCK_RTT_MS),
    }


def build_status(
    snapshot: SensorSnapshot,
    stats: StreamStats,
    recording: RecordingInfo,
    rejected: int,
    now_mono: float,
    destination: Destination | None = None,
    goal: Goal | None = None,
    external_camera: bool = False,
) -> dict[str, Any]:
    frame_fresh = is_fresh(snapshot.frame, FRAME_STALE_MS, now_mono)
    gps_fresh = is_fresh(snapshot.gps, GPS_STALE_MS, now_mono)
    compass_fresh = is_fresh(snapshot.heading, HEADING_STALE_MS, now_mono)
    # Only the connected phone's clock counts; a departed phone's estimate lingers
    # until the next one attaches.
    clock_synced = snapshot.phone_connected and is_synced(snapshot.clock, MAX_CLOCK_RTT_MS)
    hello = snapshot.hello
    source = _frame_source(snapshot, clock_synced, external_camera)
    camera_hello = snapshot.camera_hello if external_camera else hello
    camera = camera_hello.camera if camera_hello else None
    return {
        "type": "status",
        "phone_connected": snapshot.phone_connected,
        "clock_synced": clock_synced,
        # A live camera, judged by capture time. With an external camera its own link
        # and clock must be live too. GPS is not required (there is none indoors); its
        # freshness is reported in "gps".
        "sensors_ok": (
            snapshot.phone_connected
            and clock_synced
            and source["connected"]
            and source["clock_synced"]
            and frame_fresh
        ),
        "frame_source": source,
        "frame": _frame(snapshot, now_mono, frame_fresh),
        "gps": _gps(snapshot, now_mono, gps_fresh),
        "heading": _heading(snapshot, now_mono, gps_fresh, compass_fresh),
        "motion": _motion(snapshot, now_mono),
        "clock": asdict(snapshot.clock) if snapshot.clock else None,
        "stats": {
            **asdict(summarize(stats, now_mono, FPS_WINDOW_S)),
            "rejected": rejected,
        },
        "camera": camera.model_dump() if camera else None,
        "mount": hello.mount.model_dump() if hello and hello.mount else None,
        "recording": asdict(recording),
        "destination": destination_status(
            destination, snapshot.gps.value if snapshot.gps and gps_fresh else None
        ),
        # The route or instruction a driving model follows; GET /api/goal has the route itself.
        "goal": goal_status(goal),
        "link": _link(snapshot, now_mono),
    }


def live_status(
    hub: Hub,
    recorder: Recorder,
    destinations: DestinationStore,
    now_mono: float,
) -> dict[str, Any]:
    """The status right now, as served by GET /api/status and every /ws/viewer message."""
    status = build_status(
        hub.snapshot,
        hub.stats,
        recorder.info,
        hub.rejected,
        now_mono,
        destination=destinations.current,
        goal=destinations.goal,
        external_camera=hub.external_camera,
    )
    status["chair"] = chair_status()  # speed estimate, link and board health from wheelchaird
    return status
