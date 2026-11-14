"""Where the rider asked to go, set from the iPad.

The service holds one trip at a time, in memory only. It has two parts:

- the **destination**, a point. The status gives its straight-line distance and
  bearing from the phone's latest fresh GPS fix, and nothing while the fix is stale.
- the **goal**, what a driving model follows to get there: either the route the
  iPad planned (a list of points ending at the destination) or, indoors where
  there is no map, the rider's instruction in words.

A destination can be set with no goal (a bare point). Setting a goal replaces
the trip: a route sets the destination to its last point, an instruction leaves
no destination.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

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


# A walking-distance route from Google is a few hundred points at most.
MAX_ROUTE_POINTS = 2000
MAX_INSTRUCTION_CHARS = 200


@dataclass(frozen=True, slots=True)
class Goal:
    """What a driving model follows: a planned route, or an instruction in words."""

    kind: Literal["route", "instruction"]
    # Counts up with every change to the trip, so a reader can tell a new goal from the old one.
    version: int
    route: tuple[Destination, ...] = ()
    instruction: str | None = None


class _RouteGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    kind: Literal["route"]
    points: list[_DestinationRequest] = Field(min_length=2, max_length=MAX_ROUTE_POINTS)


class _InstructionGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    kind: Literal["instruction"]
    text: str = Field(max_length=MAX_INSTRUCTION_CHARS)

    @field_validator("text")
    @classmethod
    def _has_words(cls, text: str) -> str:
        text = " ".join(text.split())
        if not text:
            raise ValueError("an instruction needs words")
        return text


_GOAL_REQUEST: TypeAdapter[_RouteGoalRequest | _InstructionGoalRequest] = TypeAdapter(
    Annotated[_RouteGoalRequest | _InstructionGoalRequest, Field(discriminator="kind")]
)


def parse_goal(body: object) -> tuple[Destination, ...] | str:
    """A route's points, or an instruction's text. Raises pydantic.ValidationError otherwise."""
    request = _GOAL_REQUEST.validate_python(body)
    if isinstance(request, _InstructionGoalRequest):
        return request.text
    return tuple(Destination(lat=point.lat, lon=point.lon) for point in request.points)


class DestinationStore:
    """The current trip. Swapped whole, never changed in place; event loop only."""

    def __init__(self) -> None:
        self._current: Destination | None = None
        self._goal: Goal | None = None
        self._version = 0

    @property
    def current(self) -> Destination | None:
        return self._current

    @property
    def goal(self) -> Goal | None:
        return self._goal

    def set(self, destination: Destination) -> None:
        """A bare point. Any route or instruction belonged to the previous trip."""
        self._current = destination
        self._goal = None

    def set_route(self, points: tuple[Destination, ...]) -> Goal:
        self._version += 1
        self._current = points[-1]
        self._goal = Goal("route", self._version, route=points)
        return self._goal

    def set_instruction(self, text: str) -> Goal:
        self._version += 1
        self._current = None
        self._goal = Goal("instruction", self._version, instruction=text)
        return self._goal

    def clear(self) -> None:
        self._current = None
        self._goal = None


def goal_status(goal: Goal | None) -> dict[str, Any] | None:
    """The goal for the status: small, since it is sent many times a second."""
    if goal is None:
        return None
    return {
        "kind": goal.kind,
        "version": goal.version,
        "instruction": goal.instruction,
        "route_points": len(goal.route),
    }


def goal_detail(goal: Goal | None) -> dict[str, Any] | None:
    """The whole goal, route included. Asked for once per change, not streamed."""
    summary = goal_status(goal)
    if summary is None or goal is None:
        return None
    return {**summary, "route": [{"lat": point.lat, "lon": point.lon} for point in goal.route]}


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
