"""Per-camera ignore regions: operator-configured areas whose detections are dropped.

A fixed camera sees fixed things. A poster, a mirror, a screen showing a face, a printed
safety notice - any of them can produce a person detection that never moves and never goes
away, and on a monitoring dashboard a permanent phantom occupant is worse than a missed one,
because it trains the operator to disbelieve the count.

The mechanism is configuration, not code. Regions are normalized to the frame, so the same
configuration survives a resolution change; there are none by default, so every camera that
has not been configured behaves exactly as before; and nothing about one camera's regions
reaches another.

**The rule: a detection is ignored when at least ``min_containment`` of its area lies inside a
single region.** Containment, not centre-in-region, and that choice is deliberate. Centre-in
is simpler but suppresses a real person the moment they stand in front of the masked object -
their box is large, its centre drifts over the poster, and the person vanishes from the count.
Containment asks instead whether the detection *is* the artifact: a poster's box sits wholly
inside the region an operator drew around the poster, while an adult standing in front of it
is taller and wider than the frame it hangs in, so most of their area is outside and they are
kept. The failure that matters is losing a person, so the rule is biased against doing that.

Ignore regions are for known fixed visual artifacts - posters, mirrors, displays, signage.
They are **not** a substitute for detector qualification: masking an area hides whatever else
is in it, so a region drawn around a doorway would hide the people coming through it. That is
why a single region may not cover more than half the frame, the regions together may not cover
more than three quarters of it, and their number is bounded.

**Nothing is suppressed invisibly (V1-03B).** Configured regions, their labels and the
containment rule are reported on the live status and dashboard and outlined on the preview, and
every suppressed detection is counted. A region is only ever applied because an operator
configured it: the live runtime may *suggest* a region for a persistent low-confidence track,
but it never applies one, because a person who stands still looks exactly like a fixed object
to a system that cannot tell them apart.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

# Enough to mask the fixed artifacts in one room; small enough that a typo in a config loop
# cannot blind a camera.
MAX_IGNORE_REGIONS = 8
# No single region may swallow half the view, and the set may not swallow three quarters of it.
# The sum of areas overestimates the union when regions overlap, which is the safe direction.
MAX_REGION_AREA_FRACTION = 0.5
MAX_TOTAL_AREA_FRACTION = 0.75
# How much of a detection must lie inside a region before it is treated as that region's
# artifact rather than as something in front of it.
DEFAULT_MIN_CONTAINMENT = 0.8
# Below this, a containment rule suppresses detections that mostly lie *outside* the region -
# a person standing in front of it. Permitted, because an operator may need it, but reported as
# a warning wherever the regions are shown.
LOW_CONTAINMENT_WARNING_BELOW = 0.5
# A region narrower or shorter than this fraction of the frame cannot contain a detection the
# validator would keep; it is a typo, not a mask.
MIN_REGION_SIDE_FRACTION = 0.005
# A label is for the operator's own notes ("poster by the door"). It is shown only on the
# loopback operator dashboard and status, never drawn into the picture, never logged with
# imagery, and never used for classification. The character set is restricted so a label can
# never carry markup into the page.
MAX_LABEL_LENGTH = 40
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9 _.()/:#-]*$")


class IgnoreRegionError(ValueError):
    """A region could not be accepted. The message names the rule, never a camera or a room."""


@dataclass(frozen=True, slots=True)
class IgnoreRegion:
    """One normalized rectangle on the frame, in ``[0, 1]`` with the origin at top-left."""

    x1: float
    y1: float
    x2: float
    y2: float
    label: str = ""

    def __post_init__(self) -> None:
        for name in ("x1", "y1", "x2", "y2"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))  # NaN and infinity are not coordinates
            ):
                raise IgnoreRegionError(f"{name} must be a finite number")
            if not 0.0 <= float(value) <= 1.0:
                raise IgnoreRegionError(
                    f"{name} must be normalized to the frame, between 0 and 1, got {value}"
                )
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise IgnoreRegionError(
                "a region needs positive width and height, with x1 < x2 and y1 < y2"
            )
        if min(self.x2 - self.x1, self.y2 - self.y1) < MIN_REGION_SIDE_FRACTION:
            raise IgnoreRegionError(
                f"a region must be at least {MIN_REGION_SIDE_FRACTION:.1%} of the frame "
                "in each dimension"
            )
        if self.area > MAX_REGION_AREA_FRACTION:
            raise IgnoreRegionError(
                f"a single region may not cover more than "
                f"{MAX_REGION_AREA_FRACTION:.0%} of the frame, this covers {self.area:.0%}"
            )
        if len(self.label) > MAX_LABEL_LENGTH:
            raise IgnoreRegionError(f"label must be at most {MAX_LABEL_LENGTH} characters")
        if not LABEL_PATTERN.match(self.label):
            raise IgnoreRegionError(
                "label may contain only letters, digits, spaces and . _ - ( ) / : #"
            )

    @property
    def area(self) -> float:
        return (self.x2 - self.x1) * (self.y2 - self.y1)

    def pixels(self, width: int, height: int) -> tuple[float, float, float, float]:
        return (self.x1 * width, self.y1 * height, self.x2 * width, self.y2 * height)

    def containment_of(self, bbox: Sequence[float], *, width: int, height: int) -> float:
        """What fraction of ``bbox``'s area lies inside this region, in ``[0, 1]``."""
        bx1, by1, bx2, by2 = (float(value) for value in bbox[:4])
        box_area = max(bx2 - bx1, 0.0) * max(by2 - by1, 0.0)
        if box_area <= 0.0:
            # A degenerate box has no area to be contained. The validator rejects these before
            # they reach here; refusing to ignore one is the conservative answer regardless.
            return 0.0
        rx1, ry1, rx2, ry2 = self.pixels(width, height)
        overlap_width = min(bx2, rx2) - max(bx1, rx1)
        overlap_height = min(by2, ry2) - max(by1, ry1)
        if overlap_width <= 0.0 or overlap_height <= 0.0:
            return 0.0
        return (overlap_width * overlap_height) / box_area

    def as_dict(self) -> dict[str, float | str]:
        return {
            "x1": round(self.x1, 4),
            "y1": round(self.y1, 4),
            "x2": round(self.x2, 4),
            "y2": round(self.y2, 4),
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class IgnoreRegionSet:
    """The regions configured for one camera. Empty is the default and costs nothing."""

    regions: tuple[IgnoreRegion, ...] = ()
    min_containment: float = DEFAULT_MIN_CONTAINMENT

    def __post_init__(self) -> None:
        if len(self.regions) > MAX_IGNORE_REGIONS:
            raise IgnoreRegionError(
                f"at most {MAX_IGNORE_REGIONS} ignore regions, got {len(self.regions)}"
            )
        if not (math.isfinite(self.min_containment) and 0.0 < self.min_containment <= 1.0):
            raise IgnoreRegionError("min_containment must be greater than 0 and at most 1")
        total = sum(region.area for region in self.regions)
        if total > MAX_TOTAL_AREA_FRACTION:
            raise IgnoreRegionError(
                f"ignore regions may not cover more than "
                f"{MAX_TOTAL_AREA_FRACTION:.0%} of the frame in total, these cover {total:.0%}"
            )

    def __bool__(self) -> bool:
        return bool(self.regions)

    def __len__(self) -> int:
        return len(self.regions)

    def matching(self, bbox: Sequence[float], *, width: int, height: int) -> IgnoreRegion | None:
        """The first region that contains enough of ``bbox``, or None to keep the detection."""
        if not self.regions or width <= 0 or height <= 0:
            return None
        for region in self.regions:
            if region.containment_of(bbox, width=width, height=height) >= self.min_containment:
                return region
        return None

    def as_dicts(self) -> list[dict[str, float | str]]:
        return [region.as_dict() for region in self.regions]

    @property
    def low_containment(self) -> bool:
        """The rule suppresses detections that are mostly outside the region."""
        return bool(self.regions) and self.min_containment < LOW_CONTAINMENT_WARNING_BELOW

    def status(self) -> dict[str, Any]:
        """Everything an operator needs to audit what is being suppressed. No pixels."""
        return {
            "count": len(self.regions),
            "regions": self.as_dicts(),
            "min_containment": round(self.min_containment, 4),
            "rule": (
                "a detection is ignored before tracking when at least min_containment of its "
                "area lies inside one region"
            ),
            "total_area_fraction": round(sum(region.area for region in self.regions), 4),
            "low_containment_warning": self.low_containment,
        }


def parse_ignore_region(text: str) -> IgnoreRegion:
    """``x1,y1,x2,y2`` or ``x1,y1,x2,y2,label`` with normalized coordinates.

    Operators type these on a command line under time pressure, so every failure names the rule
    that was broken rather than raising a parse error about a list index.
    """
    parts = [part.strip() for part in str(text).split(",")]
    if len(parts) not in (4, 5):
        raise IgnoreRegionError(
            "expected x1,y1,x2,y2 (normalized 0-1), optionally followed by a label"
        )
    numbers: list[float] = []
    for name, part in zip(("x1", "y1", "x2", "y2"), parts[:4], strict=True):
        try:
            numbers.append(float(part))
        except ValueError:
            raise IgnoreRegionError(f"{name} is not a number: {part!r}") from None
    label = parts[4] if len(parts) == 5 else ""
    return IgnoreRegion(x1=numbers[0], y1=numbers[1], x2=numbers[2], y2=numbers[3], label=label)


def build_ignore_regions(
    texts: Iterable[str] | None, *, min_containment: float = DEFAULT_MIN_CONTAINMENT
) -> IgnoreRegionSet:
    """Parse a whole configuration. No regions configured means no behaviour change."""
    if not texts:
        return IgnoreRegionSet()
    return IgnoreRegionSet(
        tuple(parse_ignore_region(text) for text in texts), min_containment=min_containment
    )
