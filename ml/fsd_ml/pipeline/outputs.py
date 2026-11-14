"""The three things one pass sends out, as plain JSON-ready dicts (docs/outputs.md)."""

from __future__ import annotations

from typing import Any

import numpy as np

from fsd_ml.geo import LatLon, to_latlon
from fsd_ml.pipeline.types import ChairStatus, Decision


def _points(path_xy: np.ndarray) -> list[list[float]]:
    return np.asarray(path_xy).round(3).tolist()


def command_message(decision: Decision) -> dict[str, Any]:
    """To the chair."""
    command = decision.command
    return {
        "type": "drive",
        "seq": command.seq,
        "t": command.t_ms,
        "v_mps": round(command.v_mps, 4),
        "w_radps": round(command.w_radps, 4),
        "ttl_ms": command.ttl_ms,
    }


def drive_state(
    decision: Decision, origin: LatLon | None, chair: ChairStatus | None = None
) -> dict[str, Any]:
    """To the iPad: the fields of ``DriveState`` (app/src/types.ts) the pipeline owns.

    The pipeline works in (ahead, left); the iPad draws (x right, z ahead).
    """
    pose = decision.pose
    position = None
    if pose is not None and origin is not None:
        lat, lon = to_latlon(pose.east, pose.north, origin)
        position = {"lat": lat, "lng": lon}
    speed = pose.speed_mps if pose else 0.0
    return {
        "mode": decision.mode,
        "position": position,
        "headingDeg": pose.heading_deg if pose else None,
        "speedMps": round(speed, 3),
        "batteryPct": chair.battery_pct if chair else None,
        "path": [{"x": round(-left, 3), "z": round(ahead, 3)} for ahead, left in decision.path_xy],
        "pathStatus": decision.path_status,
        "obstacles": [
            {"id": o.id, "kind": o.kind, "x": round(-o.y, 3), "z": round(o.x, 3)}
            for o in decision.obstacles
        ],
        "yieldingToId": decision.yielding_to_id,
        "reason": decision.reason,
        "takeOver": decision.take_over,
        "engageBlocked": decision.engage_blocked,
        "progressM": round(decision.progress_m, 2),
        "remainingM": round(decision.remaining_m, 2),
        "etaSeconds": round(decision.remaining_m / speed) if speed > 0.1 else None,
    }


def record(decision: Decision, chair: ChairStatus | None = None) -> dict[str, Any]:
    """To disk: one line per pass, enough to replay and explain the decision."""
    pose = decision.pose
    return {
        "t": decision.command.t_ms,
        "mode": decision.mode,
        "pose": None
        if pose is None
        else {
            "east": round(pose.east, 3),
            "north": round(pose.north, 3),
            "heading_deg": round(pose.heading_deg, 2),
            "speed_mps": round(pose.speed_mps, 3),
            "yaw_rate_radps": round(pose.yaw_rate_radps, 4),
        },
        "progress_m": round(decision.progress_m, 2),
        "remaining_m": round(decision.remaining_m, 2),
        "model": {
            "name": decision.model_name,
            "age_ms": decision.model_age_ms,
            "inference_ms": decision.inference_ms,
        },
        "candidates": [
            {"score": score, "path_xy": _points(path)} for score, path in decision.candidates
        ],
        "chosen_index": decision.chosen_index,
        "path_status": decision.path_status,
        "obstacles": [
            {"id": o.id, "kind": o.kind, "x": round(o.x, 3), "y": round(o.y, 3), "r": o.radius_m}
            for o in decision.obstacles
        ],
        "yielding_to_id": decision.yielding_to_id,
        "reason": decision.reason,
        "gate_reason": decision.gate_reason,
        "take_over": decision.take_over,
        "command": command_message(decision),
        "chair": None
        if chair is None
        else {
            "mode": chair.mode,
            "joystick_active": chair.joystick_active,
            "estop": chair.estop,
            "applied_seq": chair.applied_seq,
        },
    }
