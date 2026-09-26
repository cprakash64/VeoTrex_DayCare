"""Doorway / portal geometry for anonymous room-transition events (V1-05A).

A person *appearing in view* is not a person *entering the room*: they may have stepped out from
behind furniture, the detector may have recovered after a miss, or they were there when the
stream started. Entry and exit need spatial evidence, and the evidence is an operator-configured
**portal**: a line segment drawn across a doorway, with one side declared to be the room.

**Coordinates.** Normalised to the frame, ``[0, 1]``, origin at the top-left, ``x`` to the
right and ``y`` *down* - the same convention as ignore regions - so a configuration survives a
resolution change. Distances below are measured in this normalised plane (fractions of the
frame's width horizontally and of its height vertically); that is anisotropic on a non-square
frame, deterministic, and independent of resolution.

**Which side is the room.** A portal is the segment ``A = (x1, y1)`` to ``B = (x2, y2)`` plus
``inside``, one of ``LEFT``, ``RIGHT``, ``ABOVE`` or ``BELOW`` *as seen on the image*: for a
doorway line running up and down the picture the room is to its LEFT or RIGHT; for one running
across the picture it is ABOVE or BELOW. Internally that becomes the unit normal of the line
pointing into the room: of the two normals ``±(dy, -dx) / |d|``, the one with a positive
component along the chosen image direction (LEFT ``(-1, 0)``, RIGHT ``(1, 0)``, ABOVE
``(0, -1)``, BELOW ``(0, 1)``). A choice that is nearly parallel to the line - LEFT for a line
running across the picture - does not say which side is meant, and is refused: the chosen
direction must make at least ``MIN_SIDE_ALIGNMENT`` (cos 60 degrees) with the normal.

The signed **inside offset** of a point ``P`` is ``(P - A) . n_inside``: positive in the room,
negative outside, zero on the line (the infinite line; see ``portal_crossing`` for how the
segment's extent is used).

**Reference point.** A person box's reference point is its bottom-centre, clamped to the frame:
for an upright person that approximates where they stand, which is what a doorway on the floor
is about. It is called ``track_reference_point``, not "feet": a box is a detector's estimate,
the bottom edge can be a knee behind a table, and nothing here claims a physical floor position.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

# A doorway, a second exterior door and a margin. A typo in a configuration loop cannot create
# hundreds of lines to evaluate on every observation.
MAX_PORTALS = 4
# Shorter than this is a typo, not a doorway: one percent of the frame.
MIN_PORTAL_LENGTH = 0.01
# How far from the line a position must be before it counts as a side (in each direction).
DEFAULT_DEADBAND = 0.02
MAX_DEADBAND = 0.1
# The chosen image direction must be at least 60 degrees away from the line itself.
MIN_SIDE_ALIGNMENT = 0.5
MAX_LABEL_LENGTH = 40
# The same restricted character set as ignore-region labels: a label is operator notes, shown
# only on the loopback operator dashboard via textContent, never drawn into the picture.
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9 _.()/:#-]*$")
# Long enough for a control-plane portal UUID, so a flag copied from the web UI keeps its id.
PORTAL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,35}$")


class PortalError(ValueError):
    """A portal could not be accepted. The message names the rule, never a camera or a room."""


class InsideSide(StrEnum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    ABOVE = "ABOVE"
    BELOW = "BELOW"


_SIDE_DIRECTIONS: dict[InsideSide, tuple[float, float]] = {
    InsideSide.LEFT: (-1.0, 0.0),
    InsideSide.RIGHT: (1.0, 0.0),
    InsideSide.ABOVE: (0.0, -1.0),
    InsideSide.BELOW: (0.0, 1.0),
}


def _finite_unit(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PortalError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):  # NaN and infinity are not coordinates
        raise PortalError(f"{name} must be a finite number")
    if not 0.0 <= number <= 1.0:
        raise PortalError(f"{name} must be normalized to the frame, between 0 and 1, got {value}")
    return number


@dataclass(frozen=True, slots=True)
class Portal:
    """One doorway line on one camera's frame, with the room on its ``inside`` side."""

    portal_id: str
    x1: float
    y1: float
    x2: float
    y2: float
    inside: InsideSide
    label: str = ""
    enabled: bool = True
    deadband: float = DEFAULT_DEADBAND

    def __post_init__(self) -> None:
        if not isinstance(self.portal_id, str) or not PORTAL_ID_PATTERN.match(self.portal_id):
            raise PortalError(
                "portal id must be 1-36 lowercase letters, digits, '-' or '_', starting with a "
                "letter or digit"
            )
        for name in ("x1", "y1", "x2", "y2"):
            _finite_unit(name, getattr(self, name))
        if not isinstance(self.inside, InsideSide):
            raise PortalError("inside must be one of LEFT, RIGHT, ABOVE or BELOW")
        if not isinstance(self.enabled, bool):
            raise PortalError("enabled must be true or false")
        if (
            isinstance(self.deadband, bool)
            or not isinstance(self.deadband, int | float)
            or not math.isfinite(float(self.deadband))
            or not 0.0 <= float(self.deadband) <= MAX_DEADBAND
        ):
            raise PortalError(f"deadband must be between 0 and {MAX_DEADBAND}")
        if not isinstance(self.label, str) or len(self.label) > MAX_LABEL_LENGTH:
            raise PortalError(f"label must be at most {MAX_LABEL_LENGTH} characters")
        if not LABEL_PATTERN.match(self.label):
            raise PortalError("label may contain only letters, digits, spaces and . _ - ( ) / : #")
        if self.length < MIN_PORTAL_LENGTH:
            raise PortalError(
                f"a portal line must be at least {MIN_PORTAL_LENGTH:.0%} of the frame long"
            )
        direction = _SIDE_DIRECTIONS[self.inside]
        normal = self._unit_normal()
        if abs(normal[0] * direction[0] + normal[1] * direction[1]) < MIN_SIDE_ALIGNMENT:
            raise PortalError(
                f"inside={self.inside} is ambiguous for this line: it runs almost along that "
                "direction. Use LEFT/RIGHT for a line running up and down the picture, "
                "ABOVE/BELOW for one running across it"
            )

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    def _unit_normal(self) -> tuple[float, float]:
        dx, dy = self.x2 - self.x1, self.y2 - self.y1
        length = math.hypot(dx, dy)
        return (dy / length, -dx / length)

    @property
    def inside_normal(self) -> tuple[float, float]:
        """The unit normal of the line pointing into the room."""
        nx, ny = self._unit_normal()
        sx, sy = _SIDE_DIRECTIONS[self.inside]
        if nx * sx + ny * sy < 0:
            return (-nx, -ny)
        return (nx, ny)

    def inside_offset(self, point: tuple[float, float]) -> float:
        """Signed distance from the line: positive inside the room, negative outside."""
        nx, ny = self.inside_normal
        return (point[0] - self.x1) * nx + (point[1] - self.y1) * ny

    def segment_parameter(self, point: tuple[float, float]) -> float:
        """Where ``point`` projects along A->B: 0 at A, 1 at B."""
        dx, dy = self.x2 - self.x1, self.y2 - self.y1
        return ((point[0] - self.x1) * dx + (point[1] - self.y1) * dy) / (dx * dx + dy * dy)

    def as_dict(self) -> dict[str, Any]:
        nx, ny = self.inside_normal
        return {
            "portal_id": self.portal_id,
            "x1": round(self.x1, 4),
            "y1": round(self.y1, 4),
            "x2": round(self.x2, 4),
            "y2": round(self.y2, 4),
            "inside": str(self.inside),
            "inside_normal": [round(nx, 4), round(ny, 4)],
            "label": self.label,
            "enabled": self.enabled,
            "deadband": round(self.deadband, 4),
        }

    def as_flag(self) -> str:
        """The ``--portal`` text that reproduces this portal exactly."""
        values = ",".join(f"{value:g}" for value in (self.x1, self.y1, self.x2, self.y2))
        text = f"{self.portal_id}:{values},{self.inside}"
        if self.label or self.deadband != DEFAULT_DEADBAND:
            text += f",{self.label}"
        if self.deadband != DEFAULT_DEADBAND:
            text += f",deadband={self.deadband:g}"
        return text


@dataclass(frozen=True, slots=True)
class PortalSet:
    """The portals configured for one camera. Empty is the default and costs nothing."""

    portals: tuple[Portal, ...] = ()

    def __post_init__(self) -> None:
        if len(self.portals) > MAX_PORTALS:
            raise PortalError(f"at most {MAX_PORTALS} portals per camera, got {len(self.portals)}")
        identifiers = [portal.portal_id for portal in self.portals]
        if len(set(identifiers)) != len(identifiers):
            raise PortalError("portal ids must be unique per camera")

    def __bool__(self) -> bool:
        return any(portal.enabled for portal in self.portals)

    def __len__(self) -> int:
        return len(self.portals)

    @property
    def enabled(self) -> tuple[Portal, ...]:
        return tuple(portal for portal in self.portals if portal.enabled)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [portal.as_dict() for portal in self.portals]


def track_reference_point(
    bbox_xyxy: Sequence[float], *, width: int, height: int
) -> tuple[float, float] | None:
    """The bottom-centre of a person box, normalised and clamped to the frame.

    Returns None for a box that is not a finite, positive-area box on a real frame: such a box
    carries no position, and inventing one would be worse than skipping the observation.
    """
    if width <= 0 or height <= 0 or len(bbox_xyxy) < 4:
        return None
    x1, y1, x2, y2 = (float(value) for value in bbox_xyxy[:4])
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
        return None
    x = min(max((x1 + x2) / 2.0 / width, 0.0), 1.0)
    y = min(max(y2 / height, 0.0), 1.0)
    return (x, y)


def parse_portal(text: str, *, index: int = 1, deadband: float = DEFAULT_DEADBAND) -> Portal:
    """``[id:]x1,y1,x2,y2,INSIDE[,label][,deadband=D]`` with normalised coordinates.

    ``INSIDE`` is LEFT, RIGHT, ABOVE or BELOW (case-insensitive). Without an ``id:`` prefix the
    portal is named ``portal-<index>``; without ``deadband=`` it uses ``deadband`` (the
    ``--portal-deadband`` value). Every failure names the rule that was broken.
    """
    raw = str(text).strip()
    portal_id = f"portal-{index}"
    # An id prefix is a colon before the first comma; a label may contain colons, but only
    # after the coordinates.
    colon, comma = raw.find(":"), raw.find(",")
    if colon != -1 and (comma == -1 or colon < comma):
        portal_id, raw = raw[:colon].strip(), raw[colon + 1 :]
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) == 7 or (len(parts) == 6 and parts[5].startswith("deadband=")):
        token = parts.pop()
        if not token.startswith("deadband="):
            raise PortalError("the seventh field must be deadband=<number>")
        try:
            deadband = float(token.removeprefix("deadband="))
        except ValueError:
            raise PortalError("deadband is not a number") from None
    if len(parts) not in (5, 6):
        raise PortalError(
            "expected [id:]x1,y1,x2,y2,INSIDE[,label] with normalized 0-1 coordinates and "
            "INSIDE one of LEFT, RIGHT, ABOVE, BELOW"
        )
    numbers: list[float] = []
    for name, part in zip(("x1", "y1", "x2", "y2"), parts[:4], strict=True):
        try:
            numbers.append(float(part))
        except ValueError:
            raise PortalError(f"{name} is not a number: {part!r}") from None
    try:
        inside = InsideSide(parts[4].upper())
    except ValueError:
        raise PortalError("INSIDE must be one of LEFT, RIGHT, ABOVE or BELOW") from None
    label = parts[5] if len(parts) == 6 else ""
    return Portal(
        portal_id, numbers[0], numbers[1], numbers[2], numbers[3], inside, label, deadband=deadband
    )


def build_portals(texts: Iterable[str] | None, *, deadband: float = DEFAULT_DEADBAND) -> PortalSet:
    """Parse a whole configuration. No portals configured means no behaviour change."""
    if not texts:
        return PortalSet()
    items = list(texts)
    if len(items) > MAX_PORTALS:
        raise PortalError(f"at most {MAX_PORTALS} portals per camera, got {len(items)}")
    return PortalSet(
        tuple(
            parse_portal(text, index=i, deadband=deadband) for i, text in enumerate(items, start=1)
        )
    )
