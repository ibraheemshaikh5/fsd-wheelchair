"""Distances and bearings between GPS points on a spherical Earth.

Good to well under a metre over the distances a wheelchair covers; not for surveying.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0
FULL_TURN_DEG = 360.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from the first point to the second.

    Clockwise from true north, in [0, 360).
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    bearing = math.degrees(math.atan2(y, x)) % FULL_TURN_DEG
    # A tiny negative angle rounds up to exactly 360.0 under %, which is out of range.
    return 0.0 if bearing >= FULL_TURN_DEG else bearing
