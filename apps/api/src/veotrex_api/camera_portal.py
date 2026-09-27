"""Camera portal (doorway line) configuration rules: the pure core (V1-05A).

A portal is an operator-configured line across a doorway in one camera's picture, with one side
declared to be the room. The live edge runtime uses it to report anonymous
``PERSON_ENTERED_ROOM`` / ``PERSON_EXITED_ROOM`` events only when a track crosses the line -
appearing in view is never an entry (ADR 0029). This module holds the same geometry rules the
edge applies (``veotrex_edge_agent.live.portal_geometry``), so the control plane never stores a
portal the edge would refuse; a parity test keeps the two sets of constants identical.

Coordinates are normalised to the frame, origin top-left, ``y`` down. ``inside`` is the room's
side *as seen on the picture*: LEFT/RIGHT for a line running up and down it, ABOVE/BELOW for one
running across it; a direction nearly parallel to the line is ambiguous and refused.

Nothing here concerns people: no identity, no classification, no image.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

MAX_PORTALS = 4
MIN_PORTAL_LENGTH = 0.01
DEFAULT_DEADBAND = 0.02
MAX_DEADBAND = 0.1
MIN_SIDE_ALIGNMENT = 0.5
MAX_LABEL_LENGTH = 40
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9 _.()/:#-]*$")


class PortalConfigError(ValueError):
    """A portal was refused. The category names the rule, never a camera or a room."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


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


@dataclass(frozen=True, slots=True)
class PortalGeometry:
    x1: float
    y1: float
    x2: float
    y2: float
    inside: InsideSide
    deadband: float = DEFAULT_DEADBAND

    def inside_normal(self) -> tuple[float, float]:
        dx, dy = self.x2 - self.x1, self.y2 - self.y1
        length = math.hypot(dx, dy)
        nx, ny = dy / length, -dx / length
        sx, sy = _SIDE_DIRECTIONS[self.inside]
        return (-nx, -ny) if nx * sx + ny * sy < 0 else (nx, ny)


def clean_label(value: object) -> str:
    if not isinstance(value, str):
        raise PortalConfigError("invalid_portal_label")
    label = " ".join(value.split())
    if not label or len(label) > MAX_LABEL_LENGTH or not LABEL_PATTERN.match(label):
        raise PortalConfigError("invalid_portal_label")
    return label


def _coordinate(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PortalConfigError("invalid_portal_coordinates")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise PortalConfigError("invalid_portal_coordinates")
    return number


def validate_geometry(
    x1: object, y1: object, x2: object, y2: object, inside: object, deadband: object
) -> PortalGeometry:
    """Every rule the edge applies, in the same order; refusals are bounded categories."""
    points = [_coordinate(value) for value in (x1, y1, x2, y2)]
    try:
        side = InsideSide(str(inside).upper()) if isinstance(inside, str) else None
    except ValueError:
        side = None
    if side is None:
        raise PortalConfigError("invalid_portal_inside")
    if (
        isinstance(deadband, bool)
        or not isinstance(deadband, int | float)
        or not math.isfinite(float(deadband))
        or not 0.0 <= float(deadband) <= MAX_DEADBAND
    ):
        raise PortalConfigError("invalid_portal_deadband")
    if math.hypot(points[2] - points[0], points[3] - points[1]) < MIN_PORTAL_LENGTH:
        raise PortalConfigError("portal_too_short")
    geometry = PortalGeometry(points[0], points[1], points[2], points[3], side, float(deadband))
    dx, dy = points[2] - points[0], points[3] - points[1]
    length = math.hypot(dx, dy)
    sx, sy = _SIDE_DIRECTIONS[side]
    if abs((dy / length) * sx + (-dx / length) * sy) < MIN_SIDE_ALIGNMENT:
        raise PortalConfigError("portal_inside_ambiguous")
    return geometry


def edge_flag(portal_id: str, geometry: PortalGeometry, label: str) -> str:
    """The ``veotrex-edge live-demo --portal`` text for local evaluation. Remote distribution
    to the edge is not connected yet; this is how an operator carries the configuration over."""
    values = ",".join(
        f"{value:g}" for value in (geometry.x1, geometry.y1, geometry.x2, geometry.y2)
    )
    text = f"{portal_id}:{values},{geometry.inside},{label}"
    if geometry.deadband != DEFAULT_DEADBAND:
        text += f",deadband={geometry.deadband:g}"
    return text
