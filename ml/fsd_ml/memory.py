"""Remember obstacles after they leave the camera's view.

The depth model only knows what is in the picture now. A thing the chair is passing
slides out of view while it is still beside the chair, and without a memory the chair
would swing back into it. This keeps every obstacle on a map fixed to the chair, moves
the map as the chair moves, and forgets a thing only when there is a reason to.

    memory = ObstacleMemory(view)
    memory.move(speed_mps, turn_radps, dt_s)     # how the chair moved since last time
    memory.see(seen_now, now_s)                  # what the depth model sees now
    memory.obstacles()                           # everything to steer round

The cases it is built for, each with a test:

- passing something: it leaves the view, stays on the map, and slides back along the
  chair's side until it is behind;
- turning on the spot: the map turns the other way about the drive wheels;
- a thing that was in view and is gone (a person walked off): cleared after a moment,
  but only where the camera can really see, and never when something nearer hides it;
- noise: a single frame's speck is used while it is seen, and not kept once it is not;
- a covered camera or a stalled depth model: nothing is seen, so nothing is cleared;
- the chair moved by hand or by the joystick: what is in view stops matching the map,
  so everything out of view is forgotten instead of trusted;
- something already inside the clearance beside the chair: it stops the chair turning
  into it, but does not freeze the chair where it stands;
- memories that sit inside the chair's own body: the map has drifted, so they go;
- nothing is kept for ever: a thing unseen for long enough, or left far behind, is dropped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from fsd_ml.pipeline.types import Obstacle

# Two sightings this close are the same thing. Less than the spacing of the depth model's
# cells, so neighbouring parts of one wall stay separate.
SAME_M = 0.1
# An unconfirmed speck is given this long to be seen again before it is dropped.
SPECK_S = 0.3
# What is in view shows how the chair really moved; this many matches are needed to trust
# it, and it never corrects the map by more than this in one look.
CORRECT_MIN = 6
CORRECT_MAX_M = 0.08
CORRECT_SMOOTH = 0.5
# A thing must be seen this many times before it is remembered once out of view.
CONFIRM_SIGHTS = 2
# In clear view and not seen for this long: it has gone.
GONE_AFTER_S = 0.6
# Far off, where the depth model is patchy, a thing flickers in and out. Unseen there for
# this long, it is let go; otherwise every flicker would leave a ghost and the map would
# fill up while the chair stands still. The sides of the picture are deliberately not
# treated this way: a thing sliding out of the side is exactly what must be remembered.
FLICKER_AFTER_S = 1.5
# Out of view and not seen for this long: too old to trust.
FORGET_AFTER_S = 20.0
# This far behind the chair's back, or this far away at all, it cannot matter.
BEHIND_M = 0.6
FAR_M = 5.0
# Remembered things are drawn larger the further the chair has moved since seeing them,
# because the chair's own speed is only roughly known.
GROW_PER_M = 0.12
GROW_PER_RAD = 0.06
GROW_MAX_M = 0.2
# The view is not trusted for clearing right at its edges.
EDGE_DEG = 6.0
HIDES_DEG = 7.0
# If most of what should be in view is missing for this long, the chair has moved in a way
# the map did not follow (pushed, or driven by the joystick).
MISMATCH_SHARE = 0.7
MISMATCH_AFTER_S = 0.4
MISMATCH_MIN = 5
KEEP = 250


@dataclass(frozen=True, slots=True)
class View:
    """Where the camera can see well enough to say "nothing is there"."""

    lens_xy: tuple[float, float] = (0.0, 0.0)
    hfov_deg: float = 80.0
    # Nearer than this even a low thing is below the picture; further than this depth is too
    # rough. A foot or a box 0.7 m off is still in the picture, so its going is believed.
    near_m: float = 0.7
    far_m: float = 3.1

    def bearing_deg(self, x: float, y: float) -> float:
        return math.degrees(math.atan2(y - self.lens_xy[1], x - self.lens_xy[0]))

    def range_m(self, x: float, y: float) -> float:
        return math.hypot(x - self.lens_xy[0], y - self.lens_xy[1])

    def far_off(self, x: float, y: float, reach_m: float = 3.8) -> bool:
        """Well inside the picture from side to side, but too far for depth to be sure of."""
        inside = abs(self.bearing_deg(x, y)) < self.hfov_deg / 2 - EDGE_DEG
        return inside and self.far_m <= self.range_m(x, y) < reach_m

    def covers(self, x: float, y: float) -> bool:
        inside = abs(self.bearing_deg(x, y)) < self.hfov_deg / 2 - EDGE_DEG
        return inside and self.near_m < self.range_m(x, y) < self.far_m


@dataclass(slots=True)
class Remembered:
    x: float
    y: float
    radius_m: float
    seen_s: float
    sights: int = 1
    # Set while it is in clear view and not being seen.
    missing_since_s: float | None = None
    # How far the chair has moved and turned since it was last seen.
    moved_m: float = 0.0
    turned_rad: float = 0.0
    in_sight: bool = True
    # Something nearer stands in front of it, so not seeing it says nothing.
    hidden: bool = False


@dataclass(slots=True)
class ObstacleMemory:
    view: View = field(default_factory=View)
    # The chair turns about the middle of its drive wheels: this far ahead of its centre.
    pivot_x: float = 0.0
    # The chair's body, for telling a drifted memory from a real one, and its clearance.
    half_length_m: float = 0.51
    half_width_m: float = 0.29
    margin_m: float = 0.3
    things: list[Remembered] = field(default_factory=list)
    _mismatch_since_s: float | None = None
    # The smoothed gap between where things were expected and where they were seen.
    _slip: tuple[float, float] = (0.0, 0.0)
    _blind: bool = False
    # Why memories were last thrown away wholesale, for the page.
    note: str | None = None

    def forget(self, why: str | None = None) -> None:
        self.things.clear()
        self._mismatch_since_s, self.note, self._slip = None, why, (0.0, 0.0)

    def move(self, speed_mps: float, turn_radps: float, dt_s: float) -> None:
        """Shift the map for the chair having driven and turned (positive left) for ``dt_s``."""
        if dt_s <= 0 or (abs(speed_mps) < 1e-4 and abs(turn_radps) < 1e-4):
            return
        dt_s = min(dt_s, 1.0)  # a long gap is a stall, not a second of steady driving
        distance, angle = speed_mps * dt_s, turn_radps * dt_s
        # The pivot moves along the middle of the turn; everything else swings round it.
        step_x, step_y = distance * math.cos(angle / 2), distance * math.sin(angle / 2)
        cos, sin = math.cos(-angle), math.sin(-angle)
        for thing in self.things:
            dx, dy = thing.x - self.pivot_x - step_x, thing.y - step_y
            thing.x = self.pivot_x + dx * cos - dy * sin
            thing.y = dx * sin + dy * cos
            if not thing.in_sight:
                thing.moved_m += abs(distance)
                thing.turned_rad += abs(angle)

    def blind(self) -> None:
        """The camera saw no floor this time (covered, or pointing at a wall): nothing is in sight.

        Nothing is cleared, because not seeing through a hand is not seeing an empty room.
        """
        self._blind = True
        for thing in self.things:
            thing.in_sight = False
            thing.missing_since_s = None

    def see(self, seen: list[Obstacle] | tuple[Obstacle, ...], now_s: float) -> None:
        """Take in what the depth model sees now. Call only when it really looked."""
        seen = list(seen)
        if self._blind:
            # Back from a blind spell: time spent unable to see is not time spent not seeing.
            self._blind = False
            for thing in self.things:
                thing.seen_s = max(thing.seen_s, now_s - FLICKER_AFTER_S + GONE_AFTER_S)
        matched: set[int] = set()
        gaps: list[tuple[float, float]] = []
        fresh: list[Remembered] = []
        for found in seen:
            near = self._nearest(found.x, found.y, matched)
            if near is None:
                fresh.append(Remembered(found.x, found.y, found.radius_m, now_s))
                continue
            thing = self.things[near]
            gaps.append((found.x - thing.x, found.y - thing.y))
            # The fresh sighting is the truth: it corrects whatever the map had drifted to.
            thing.x, thing.y, thing.radius_m = found.x, found.y, found.radius_m
            thing.sights += 1
            thing.seen_s, thing.missing_since_s = now_s, None
            thing.moved_m = thing.turned_rad = 0.0
            thing.in_sight, thing.hidden = True, False
            matched.add(near)
        self._correct(gaps, matched)
        for thing in fresh:
            self.things.append(thing)
            matched.add(len(self.things) - 1)

        expected = missing = 0
        for index, thing in enumerate(self.things):
            if index in matched:
                continue
            thing.in_sight = False
            thing.hidden = self._hidden(thing, seen)
            if self.view.covers(thing.x, thing.y) and not thing.hidden:
                expected += 1
                missing += 1
                thing.missing_since_s = thing.missing_since_s or now_s
            else:
                thing.missing_since_s = None
        expected += sum(1 for i in matched if self.view.covers(self.things[i].x, self.things[i].y))
        self._check_agreement(expected, missing, now_s)
        self._drop(now_s)

    def _correct(self, gaps: list[tuple[float, float]], matched: set[int]) -> None:
        """Move what is out of view by however far the things in view were off.

        The chair's speed is only roughly known. If everything in view sits a little ahead
        of where the map put it, the chair moved less than it was told to, and the things
        that can no longer be seen are off by the same amount.
        """
        if len(gaps) < CORRECT_MIN:
            return
        xs, ys = sorted(g[0] for g in gaps), sorted(g[1] for g in gaps)
        middle = len(gaps) // 2
        keep = 1 - CORRECT_SMOOTH
        slip_x = CORRECT_SMOOTH * self._slip[0] + keep * xs[middle]
        slip_y = CORRECT_SMOOTH * self._slip[1] + keep * ys[middle]
        self._slip = (slip_x, slip_y)
        shift_x = max(-CORRECT_MAX_M, min(CORRECT_MAX_M, slip_x))
        shift_y = max(-CORRECT_MAX_M, min(CORRECT_MAX_M, slip_y))
        for index, thing in enumerate(self.things):
            if index not in matched:
                thing.x += shift_x
                thing.y += shift_y

    def _nearest(self, x: float, y: float, taken: set[int]) -> int | None:
        best, at = SAME_M, None
        for index, thing in enumerate(self.things):
            if index in taken:
                continue
            distance = math.hypot(thing.x - x, thing.y - y)
            if distance < best:
                best, at = distance, index
        return at

    def _hidden(self, thing: Remembered, seen: list[Obstacle]) -> bool:
        """Something seen now stands nearer on the same line of sight."""
        bearing, away = self.view.bearing_deg(thing.x, thing.y), self.view.range_m(thing.x, thing.y)
        return any(
            abs(self.view.bearing_deg(o.x, o.y) - bearing) < HIDES_DEG
            and self.view.range_m(o.x, o.y) < away - 0.1
            for o in seen
        )

    def _check_agreement(self, expected: int, missing: int, now_s: float) -> None:
        if expected >= MISMATCH_MIN and missing / expected >= MISMATCH_SHARE:
            self._mismatch_since_s = self._mismatch_since_s or now_s
            if now_s - self._mismatch_since_s >= MISMATCH_AFTER_S:
                # The view no longer matches the map: the chair moved in a way the map did
                # not follow. What is in sight is still true; the rest cannot be trusted.
                self.things = [t for t in self.things if t.in_sight]
                self._mismatch_since_s = None
                self.note = "The view stopped matching the map; forgot what was out of view."
        else:
            self._mismatch_since_s = None

    def _drop(self, now_s: float) -> None:
        kept = []
        for thing in self.things:
            gone = thing.missing_since_s is not None and (
                now_s - thing.missing_since_s >= GONE_AFTER_S
            )
            unconfirmed = (
                not thing.in_sight
                and thing.sights < CONFIRM_SIGHTS
                and now_s - thing.seen_s > SPECK_S
            )
            stale = now_s - thing.seen_s > FORGET_AFTER_S
            flicker = (
                not thing.in_sight
                and now_s - thing.seen_s > FLICKER_AFTER_S
                and self.view.far_off(thing.x, thing.y)
                and not thing.hidden
                and not self._blind
            )
            behind = thing.x < -self.half_length_m - BEHIND_M
            far = math.hypot(thing.x, thing.y) > FAR_M
            # Nothing can be inside the chair: a memory that is has drifted there.
            inside = (
                not thing.in_sight
                and abs(thing.x) < self.half_length_m - 0.05
                and abs(thing.y) < self.half_width_m - 0.05
            )
            if not (gone or unconfirmed or stale or flicker or behind or far or inside):
                kept.append(thing)
        kept.sort(key=lambda t: math.hypot(t.x, t.y))
        self.things = kept[:KEEP]

    def obstacles(self) -> list[Obstacle]:
        """Everything to steer round: what is seen now, and what is remembered.

        A remembered thing already inside the clearance beside the chair is moved out to the
        clearance's edge. It still stops the chair turning into it; it no longer makes every
        path start in a collision, which would freeze the chair beside whatever it is passing.
        """
        out = []
        for index, thing in enumerate(self.things):
            if not thing.in_sight and thing.sights < CONFIRM_SIGHTS:
                continue  # a speck seen once: not worth steering round once it is not seen
            grow = 0.0
            if not thing.in_sight:
                grow = min(GROW_MAX_M, GROW_PER_M * thing.moved_m + GROW_PER_RAD * thing.turned_rad)
            x, y, radius = thing.x, thing.y, thing.radius_m + grow
            # Beside the chair or off one of its corners, but not straight ahead or behind.
            alongside = abs(x) <= self.half_length_m + self.margin_m + radius
            off_to_a_side = abs(y) >= self.half_width_m - 0.05
            edge = self.half_width_m + self.margin_m + radius + 0.02
            if not thing.in_sight and alongside and off_to_a_side and abs(y) < edge:
                y = math.copysign(edge, y)
            out.append(Obstacle(f"{'s' if thing.in_sight else 'm'}{index}", "object", x, y, radius))
        return out

    def listed(self) -> list[list[float]]:
        """For the page and the record: [ahead, left, radius, 1 if seen now else 0]."""
        return [
            [round(o.x, 3), round(o.y, 3), round(o.radius_m, 3), 1 if o.id.startswith("s") else 0]
            for o in self.obstacles()
        ]
