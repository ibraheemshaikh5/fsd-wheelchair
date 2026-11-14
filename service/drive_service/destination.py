"""Where the rider asked to go, set from the iPad.

The service holds one destination at a time, in memory only. It is a point, not
a route: the status gives the straight-line distance and bearing from the
phone's latest fresh GPS fix, and nothing about it while the fix is stale.
Route planning comes later and will read the destination from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from drive_service.geo import bearing_deg, distance_m
from drive_service.protocol import Gps


@dataclass(frozen=True, slots=True)
class Destination:
    lat: float
    lon: float


class _DestinationRequest(BaseModel):
    """Body of POST /api/destination. Unknown fields, strings and NaN are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


def parse_destination(body: object) -> Destination:
    """Validate a request body. Raises pydantic.ValidationError when it is not a point."""
    request = _DestinationRequest.model_validate(body)
    return Destination(lat=request.lat, lon=request.lon)


class DestinationStore:
    """The current destination. Swapped whole, never changed in place; event loop only."""

    def __init__(self) -> None:
        self._current: Destination | None = None

    @property
    def current(self) -> Destination | None:
        return self._current

    def set(self, destination: Destination) -> None:
        self._current = destination

    def clear(self) -> None:
        self._current = None


def destination_status(
    destination: Destination | None, fresh_fix: Gps | None
) -> dict[str, Any] | None:
    """The destination for the status, measured from ``fresh_fix`` (pass None when it is stale)."""
    if destination is None:
        return None
    measured = fresh_fix is not None
    return {
        "lat": destination.lat,
        "lon": destination.lon,
        "distance_m": (
            distance_m(fresh_fix.lat, fresh_fix.lon, destination.lat, destination.lon)
            if measured
            else None
        ),
        "bearing_deg": (
            bearing_deg(fresh_fix.lat, fresh_fix.lon, destination.lat, destination.lon)
            if measured
            else None
        ),
    }
