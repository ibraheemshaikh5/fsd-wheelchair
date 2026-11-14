"""Choose which heading source to trust, in true north wherever possible.

GPS course is only meaningful while the chair is moving; the compass works
when stopped but is disturbed by the motors and frame. GPS course is relative
to true north and the compass to magnetic north, so the compass is corrected by
the local magnetic declination. Without a declination the compass is returned
as magnetic and labelled so: a caller steering in true north must refuse it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from drive_service.protocol import Gps, Heading

HeadingSource = Literal["gps_course", "compass"]
HeadingReference = Literal["true", "magnetic"]

FULL_CIRCLE_DEG = 360.0


@dataclass(frozen=True, slots=True)
class HeadingEstimate:
    deg: float
    source: HeadingSource
    reference: HeadingReference


def magnetic_to_true(magnetic_deg: float, declination_deg: float) -> float:
    """Convert a magnetic bearing to true north. Declination is east positive."""
    return (magnetic_deg + declination_deg) % FULL_CIRCLE_DEG


def _compass_estimate(compass: Heading, declination_deg: float | None) -> HeadingEstimate:
    if declination_deg is None:
        return HeadingEstimate(deg=compass.magnetic_deg, source="compass", reference="magnetic")
    return HeadingEstimate(
        deg=magnetic_to_true(compass.magnetic_deg, declination_deg),
        source="compass",
        reference="true",
    )


def best_heading(
    gps: Gps | None,
    compass: Heading | None,
    moving_speed_mps: float,
    declination_deg: float | None = None,
) -> HeadingEstimate | None:
    """Pick a heading from readings the caller has already checked are fresh.

    ``declination_deg`` (east positive) turns the compass into true north; pass
    None when it is unknown and the compass is reported as magnetic.
    """
    moving = (
        gps is not None
        and gps.course_deg is not None
        and gps.speed_mps is not None
        and gps.speed_mps >= moving_speed_mps
    )
    if moving:
        return HeadingEstimate(deg=gps.course_deg, source="gps_course", reference="true")
    if compass is not None:
        return _compass_estimate(compass, declination_deg)
    return None
