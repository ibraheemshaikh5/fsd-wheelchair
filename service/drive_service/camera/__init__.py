"""Frames from an external camera instead of the phone (sender: scripts/camera_sender.py).

``build_camera_router`` returns the /ws/camera router. The hub must be built with
``external_camera=settings.source == "external"``.
"""

from __future__ import annotations

from drive_service.camera.config import CameraSettings, FrameSource
from drive_service.camera.sockets import build_camera_router

__all__ = ["CameraSettings", "FrameSource", "build_camera_router"]
