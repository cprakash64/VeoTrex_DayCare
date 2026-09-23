"""Track domain model for recorded-video processing (V1-02B1A).

Four ideas are kept deliberately separate, and the separation is the point of this module:

``DetectedPerson``
    One box in one frame. An *observation*, not a person and not an identity. It says "the
    detector thinks a person is here, now", and nothing about who or about any other frame.

``TrackObservation``
    That box after the tracker has decided which ongoing track it belongs to. It carries a
    track id, which asserts temporal continuity within one processing run and nothing more.

``TrackSummary``
    What a whole track looked like once it ended: when it started, when it stopped, how many
    observations it had, and why it ended.

``TrackIdentityObservation``
    An OPTIONAL annotation a later stage may attach to an already-existing track, only for
    enrolled consenting staff. Nothing in this stage produces one.

DETECTION != TRACKING != IDENTITY != EVENT. A daycare event ("a teacher left the room") is
business logic derived later from temporal evidence; it is not a track ending, and this module
deliberately provides no vocabulary that would let one be mistaken for the other.

Nothing here holds pixels. A track record carries geometry, timing and counts - never a face
crop, never an embedding, never a frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

# Bumped whenever the shape of an emitted record changes in a way a consumer must notice.
TRACK_SCHEMA_VERSION = 1

Box = tuple[float, float, float, float]


class TrackLifecycle(StrEnum):
    """Lifecycle *facts* about a track, carrying no daycare meaning.

    A track starting means the tracker began following a person-shaped thing; it does NOT mean
    somebody entered the room, and a track ending does NOT mean somebody left - they may have
    been occluded, turned away, or walked behind furniture. Entry and exit require a configured
    doorway or line-crossing semantic, which this stage does not have and does not fake.
    """

    TRACK_STARTED = "TRACK_STARTED"
    TRACK_ACTIVE = "TRACK_ACTIVE"
    TRACK_ENDED = "TRACK_ENDED"


class TrackEndReason(StrEnum):
    """Why a track stopped. ``ABSENT`` is the ordinary case: the person was not matched for
    longer than the configured tolerance. ``STREAM_ENDED`` means the video simply ran out
    while the track was still live, which is not the same thing and must not be counted as a
    disappearance."""

    ABSENT = "ABSENT"
    STREAM_ENDED = "STREAM_ENDED"


@dataclass(frozen=True, slots=True)
class DetectedPerson:
    """One detector box in one frame, in source-image pixel coordinates.

    ``label`` exists to make the person-only contract explicit at the boundary rather than
    implicit in a class index: a detector that can emit other classes must filter them out
    before returning, so nothing downstream has to know what a COCO index means.
    """

    bbox_xyxy: Box
    confidence: float
    frame_index: int
    timestamp_ms: float
    label: Literal["person"] = "person"


@dataclass(frozen=True, slots=True)
class TrackObservation:
    """One frame's view of one track."""

    track_id: int
    frame_index: int
    timestamp_ms: float
    bbox_xyxy: Box
    detection_confidence: float
    track_state: str
    lifecycle: TrackLifecycle


@dataclass(frozen=True, slots=True)
class TrackSummary:
    """A completed track. Emitted once, when the track ends."""

    track_id: int
    first_seen_ms: float
    last_seen_ms: float
    observation_count: int
    maximum_confidence: float
    end_reason: TrackEndReason

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.last_seen_ms - self.first_seen_ms)


@dataclass(frozen=True, slots=True)
class TrackIdentityObservation:
    """The integration boundary for a LATER stage (V1-02B1B). Nothing here produces one.

    Shaped so that identity can only ever be an annotation *onto* an existing track: it
    carries a ``track_id`` that must already exist, and it cannot create or influence a track.
    ``staff_profile_id`` is populated only for a MATCH against an enrolled, consenting staff
    member; UNKNOWN is the default and carries no id.

    UNKNOWN means "not identified". It does NOT mean "child", and no consumer may infer one
    from the other - most UNKNOWN tracks will be adults the system has no reason to name.
    """

    track_id: int
    decision: Literal["MATCH", "UNKNOWN"]
    timestamp_ms: float
    staff_profile_id: str | None = None
    confidence: float | None = None
    model_id: str | None = None
    model_version: str | None = None

    def __post_init__(self) -> None:
        if self.decision == "UNKNOWN" and self.staff_profile_id is not None:
            # An identity that was refused must not travel with the name it refused to give.
            raise ValueError("unknown_identity_cannot_carry_a_staff_profile")
        if self.decision == "MATCH" and self.staff_profile_id is None:
            raise ValueError("match_requires_a_staff_profile")
