"""Frames of reference.

World: metres east and north of an origin (a flat patch of ground; fine over a
trip of a few kilometres).
Chair: metres ahead (x) and to the left (y) of the chair.
Heading: degrees clockwise from north, as everywhere else in the repo.
"""

from __future__ import annotations

import math

import numpy as np

EARTH_RADIUS_M = 6_371_008.8

LatLon = tuple[float, float]


def to_local(lat: float, lon: float, origin: LatLon) -> tuple[float, float]:
    """(east, north) in metres from ``origin``."""
    east = math.radians(lon - origin[1]) * EARTH_RADIUS_M * math.cos(math.radians(origin[0]))
    north = math.radians(lat - origin[0]) * EARTH_RADIUS_M
    return east, north


def to_latlon(east: float, north: float, origin: LatLon) -> LatLon:
    lat = origin[0] + math.degrees(north / EARTH_RADIUS_M)
    lon = origin[1] + math.degrees(east / (EARTH_RADIUS_M * math.cos(math.radians(origin[0]))))
    return lat, lon


def world_to_chair(
    points_en: np.ndarray, east: float, north: float, heading_deg: float
) -> np.ndarray:
    """World points, shape (n, 2), as (ahead, left) of a chair at this pose."""
    heading = math.radians(heading_deg)
    delta = np.asarray(points_en, dtype=np.float64).reshape(-1, 2) - (east, north)
    ahead = delta[:, 0] * math.sin(heading) + delta[:, 1] * math.cos(heading)
    left = -delta[:, 0] * math.cos(heading) + delta[:, 1] * math.sin(heading)
    return np.column_stack([ahead, left])


def chair_to_world(
    points_xy: np.ndarray, east: float, north: float, heading_deg: float
) -> np.ndarray:
    """(ahead, left) points of a chair at this pose, as world (east, north)."""
    heading = math.radians(heading_deg)
    points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    world_east = east + points[:, 0] * math.sin(heading) - points[:, 1] * math.cos(heading)
    world_north = north + points[:, 0] * math.cos(heading) + points[:, 1] * math.sin(heading)
    return np.column_stack([world_east, world_north])
