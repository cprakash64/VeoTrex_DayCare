"""Local recognition qualification service (V1-02B0). EVALUATION ONLY.

Answers one question - "which enrolled teacher, if any, is this photo of?" - for an operator
running a supervised test with consenting adults. It is deliberately a separate service from
``StaffEnrollmentService``: recognising someone and enrolling someone are different powers,
and nothing here can create a profile, accept an image, write a template or touch a file.

The query image is validated by exactly the same code path as an enrollment photo, so the
qualification measures the pipeline that will actually run. The bytes live in the request
handler's memory for the duration of one call and are never written anywhere; the vector
derived from them is never persisted, never logged and never returned.

Candidate selection is the safety boundary, and it is applied in SQL rather than in Python so
that a filter cannot be forgotten later:

* the tenant comes from the caller's identity mapping, under the same RLS context as every
  other staff read, so another tenant's teachers are not merely filtered out - they are not
  visible to the query at all;
* only ACTIVE profiles, so a deactivated teacher cannot be recognised;
* only profiles whose enrollment state is READY, so a half-enrolled person cannot be named;
* only ACTIVE templates, so a revoked template cannot contribute;
* only templates whose model id, model version and template version match the running
  backend, so vectors from a different model are never compared against each other.

A soft-deleted profile is excluded by all three of the first filters at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission
from veotrex_api.face_backend import FaceBackendError, FaceRecognitionBackend
from veotrex_api.face_matching import (
    MAX_CANDIDATE_TEMPLATES,
    StaffCandidate,
    TemplateDecodeError,
    Vector,
    decode,
    recognize,
)
from veotrex_api.models import StaffFaceTemplate, StaffProfile
from veotrex_api.staff_media import EnrollmentImageRejected, validate_enrollment_image
from veotrex_api.staff_service import (
    MAX_STAFF_PER_TENANT,
    REJECTION_CATEGORIES,
    StaffError,
)


@dataclass(frozen=True, slots=True)
class RecognitionTestResult:
    """What the operator is shown. No vector, no template, no landmark, no image."""

    decision: str
    staff_id: UUID | None
    display_name: str | None
    score: float
    runner_up_score: float | None
    reason: str | None
    candidates: int
    model_id: str
    model_version: str
    threshold: float
    margin: float


class StaffRecognitionService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        backend: FaceRecognitionBackend,
        *,
        max_image_bytes: int,
        threshold: float,
        margin: float,
    ) -> None:
        self._factory = factory
        self._backend = backend
        self._max_image_bytes = max_image_bytes
        self._threshold = threshold
        self._margin = margin
        self._logger = structlog.get_logger()

    async def _candidates(
        self, session: AsyncSession, tenant_id: UUID
    ) -> tuple[StaffCandidate, ...]:
        rows = (
            await session.execute(
                select(
                    StaffProfile.id,
                    StaffProfile.display_name,
                    StaffFaceTemplate.template,
                    StaffFaceTemplate.dimensions,
                    StaffFaceTemplate.dtype,
                )
                .join(
                    StaffFaceTemplate,
                    (StaffFaceTemplate.staff_profile_id == StaffProfile.id)
                    & (StaffFaceTemplate.tenant_id == StaffProfile.tenant_id),
                )
                .where(
                    StaffProfile.tenant_id == tenant_id,
                    StaffProfile.status == "ACTIVE",
                    StaffProfile.enrollment_state == "READY",
                    StaffFaceTemplate.status == "ACTIVE",
                    StaffFaceTemplate.model_id == self._backend.model_id,
                    StaffFaceTemplate.model_version == self._backend.model_version,
                    StaffFaceTemplate.template_version == self._backend.template_version,
                )
                # Total and stable: the comparison order, and therefore the tie-break in
                # ``recognize``, does not depend on how PostgreSQL happened to return rows.
                .order_by(StaffProfile.id, StaffFaceTemplate.id)
                .limit(MAX_STAFF_PER_TENANT * MAX_CANDIDATE_TEMPLATES)
            )
        ).all()
        grouped: dict[UUID, tuple[str, list[Vector]]] = {}
        for staff_id, display_name, data, dimensions, dtype in rows:
            name, vectors = grouped.setdefault(staff_id, (display_name, []))
            if len(vectors) >= MAX_CANDIDATE_TEMPLATES:
                continue
            try:
                vectors.append(decode(bytes(data), dimensions=dimensions, dtype=dtype))
            except TemplateDecodeError as exc:
                # One unusable row must not make the whole tenant unrecognisable, and it must
                # not silently become a comparison either: drop it and say so without naming
                # the person it belonged to.
                self._logger.warning("staff_template_unusable", category=exc.category)
        return tuple(
            StaffCandidate(staff_id, name, tuple(vectors))
            for staff_id, (name, vectors) in sorted(grouped.items(), key=lambda item: str(item[0]))
            if vectors
        )

    async def recognize_image(
        self, principal: AuthenticatedPrincipal, data: bytes
    ) -> RecognitionTestResult:
        """Validate, detect, embed and compare one test image. Nothing is persisted."""
        if Permission.MANAGE_STAFF not in principal.permissions:
            raise StaffError("access_denied")
        validated = validate_enrollment_image(data, max_bytes=self._max_image_bytes)
        try:
            query = self._backend.extract_query(validated.image)
        except FaceBackendError as exc:
            # The same bounded vocabulary as an enrollment upload, so a test photo is refused
            # for the same stated reasons and the dashboard needs no second set of messages.
            raise EnrollmentImageRejected(
                exc.category if exc.category in REJECTION_CATEGORIES else "template_failed"
            ) from None
        except TemplateDecodeError:
            raise EnrollmentImageRejected("template_failed") from None

        async with self._factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(principal.tenant_id)},
            )
            candidates = await self._candidates(session, principal.tenant_id)
        result = recognize(query.vector, candidates, threshold=self._threshold, margin=self._margin)
        # Decision and scores only. No display name, no staff id, no image, no vector: an
        # evaluation log must not become a record of who was seen when.
        self._logger.info(
            "staff_recognition_test",
            decision=result.decision,
            score=round(result.score, 4),
            candidates=len(candidates),
            reason=result.reason,
        )
        return RecognitionTestResult(
            result.decision,
            result.staff_id,
            result.display_name,
            round(result.score, 4),
            None if result.runner_up_score is None else round(result.runner_up_score, 4),
            result.reason,
            len(candidates),
            self._backend.model_id,
            self._backend.model_version,
            self._threshold,
            self._margin,
        )
