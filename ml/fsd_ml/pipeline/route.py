"""A planned route, and where the chair is along it."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from fsd_ml.geo import LatLon, to_local

# A new position may move progress back by at most this much, or ahead by at most
# this much, so a noisy fix cannot jump the chair onto a different leg of the route.
MAX_BACKTRACK_M = 5.0
MAX_AHEAD_M = 30.0


class Route:
    def __init__(self, points: Sequence[LatLon]) -> None:
        if len(points) < 2:
            raise ValueError("a route needs at least two points")
        self.origin: LatLon = points[0]
        xy = np.array([to_local(lat, lon, self.origin) for lat, lon in points])
        # Repeated points would make zero-length legs.
        keep = np.ones(len(xy), dtype=bool)
        keep[1:] = np.linalg.norm(np.diff(xy, axis=0), axis=1) > 1e-6
        self.xy = xy[keep]
        if len(self.xy) < 2:
            raise ValueError("a route needs at least two distinct points")
        self._legs = np.linalg.norm(np.diff(self.xy, axis=0), axis=1)
        self._starts = np.concatenate([[0.0], np.cumsum(self._legs)])
        self.length_m = float(self._starts[-1])

    def project(self, east: float, north: float, near_m: float | None = None) -> float:
        """Distance along the route of the point on it closest to (east, north).

        With ``near_m`` (the last known progress), only the stretch around it is
        searched.
        """
        position = np.array([east, north])
        low = 0.0 if near_m is None else max(0.0, near_m - MAX_BACKTRACK_M)
        high = self.length_m if near_m is None else min(self.length_m, near_m + MAX_AHEAD_M)

        best_distance = np.inf
        best_along = low
        for index, leg in enumerate(self._legs):
            start_along = self._starts[index]
            if start_along + leg < low or start_along > high:
                continue
            start, end = self.xy[index], self.xy[index + 1]
            fraction = float(np.dot(position - start, end - start) / (leg * leg))
            along = start_along + min(1.0, max(0.0, fraction)) * leg
            along = min(high, max(low, along))
            distance = float(np.linalg.norm(position - self.point_at(along)))
            if distance < best_distance:
                best_distance, best_along = distance, along
        return float(best_along)

    def point_at(self, along_m: float) -> np.ndarray:
        """World (east, north) at this distance along the route."""
        along = min(self.length_m, max(0.0, along_m))
        index = min(
            int(np.searchsorted(self._starts, along, side="right")) - 1, len(self._legs) - 1
        )
        fraction = (along - self._starts[index]) / self._legs[index]
        return self.xy[index] + fraction * (self.xy[index + 1] - self.xy[index])
