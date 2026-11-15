"""Local magnetic declination, so the compass can be turned into true north.

The value comes from the World Magnetic Model (WMM2025, via pygeomag) at the
latest fresh GPS fix. It changes by far less than a degree over a few
kilometres, so it is cached and only recomputed once the chair has moved more
than ``RECOMPUTE_DISTANCE_M`` from where it was last computed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache

from pygeomag import GeoMag

from drive_service.geo import distance_m
from drive_service.protocol import Gps

logger = logging.getLogger(__name__)

RECOMPUTE_DISTANCE_M = 5_000.0
# The WMM accepts altitudes from -1 km to 850 km.
WMM_MIN_ALTITUDE_KM = -1.0
WMM_MAX_ALTITUDE_KM = 850.0
METRES_PER_KM = 1_000.0
WMM_COEFFICIENTS = "wmm/WMM_2025.COF"

DeclinationModel = Callable[[float, float, float | None, datetime], float | None]


@cache
def _geomag() -> GeoMag:
    """Load the model coefficients once, on first use."""
    return GeoMag(coefficients_file=WMM_COEFFICIENTS)


def _decimal_year(when: datetime) -> float:
    start = datetime(when.year, 1, 1, tzinfo=UTC)
    end = datetime(when.year + 1, 1, 1, tzinfo=UTC)
    return when.year + (when - start).total_seconds() / (end - start).total_seconds()


def _altitude_km(altitude_m: float | None) -> float:
    if altitude_m is None:
        return 0.0
    return min(max(altitude_m / METRES_PER_KM, WMM_MIN_ALTITUDE_KM), WMM_MAX_ALTITUDE_KM)


def wmm_declination_deg(
    lat: float, lon: float, altitude_m: float | None, when: datetime
) -> float | None:
    """Declination in degrees, east positive, or None if the model cannot give one."""
    try:
        result = _geomag().calculate(
            glat=lat, glon=lon, alt=_altitude_km(altitude_m), time=_decimal_year(when)
        )
    except (ValueError, ArithmeticError) as error:
        logger.warning(
            "magnetic declination unavailable at %.4f, %.4f on %s: %s",
            lat,
            lon,
            when.date().isoformat(),
            error,
        )
        return None
    return float(result.d)


@dataclass(frozen=True, slots=True)
class _Entry:
    lat: float
    lon: float
    declination_deg: float | None


class DeclinationCache:
    """Declination at the last place it was computed, recomputed after a long move."""

    def __init__(
        self,
        compute: DeclinationModel = wmm_declination_deg,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._compute = compute
        self._now = now
        self._entry: _Entry | None = None
        self._lock = threading.Lock()

    def lookup(self, fix: Gps | None) -> float | None:
        """Declination for a fresh fix, or the last known one when there is no fix.

        A failed computation is remembered too, so it is not retried (and logged)
        on every call, only after the chair has moved.
        """
        with self._lock:
            if fix is not None and self._is_stale_for(fix):
                self._entry = _Entry(
                    lat=fix.lat,
                    lon=fix.lon,
                    declination_deg=self._compute(fix.lat, fix.lon, fix.altitude_m, self._now()),
                )
            return self._entry.declination_deg if self._entry else None

    def clear(self) -> None:
        with self._lock:
            self._entry = None

    def _is_stale_for(self, fix: Gps) -> bool:
        if self._entry is None:
            return True
        moved = distance_m(self._entry.lat, self._entry.lon, fix.lat, fix.lon)
        return moved > RECOMPUTE_DISTANCE_M


DEFAULT_CACHE = DeclinationCache()


def resolve_declination(
    fresh_fix: Gps | None, cache: DeclinationCache = DEFAULT_CACHE
) -> float | None:
    """The World Magnetic Model's declination at the fix; None until there is one."""
    return cache.lookup(fresh_fix)
