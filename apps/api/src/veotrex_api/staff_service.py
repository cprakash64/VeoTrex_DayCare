"""Staff (adult teacher) enrollment lifecycle (V1-02A).

Every operation runs under the caller's tenant (RLS context plus explicit predicates), checks
the caller's permission, and writes an audit event for lifecycle changes. Readiness is the
backend's decision, persisted on the profile:

    EMPTY       no accepted image
    COLLECTING  1..MIN-1 accepted images, or under the maximum but not yet complete
    PROCESSING  transient while a template is being produced inside the upload transaction
    READY       >= MIN accepted images, each with an ACTIVE template of the configured model
    FAILED      an accepted image has no usable template for the configured model

``status`` is the operator's decision (ACTIVE / INACTIVE / DELETED) and is independent of
readiness; a profile is recognisable only when status is ACTIVE and state is READY. Deleting a
profile revokes every template, marks every image DELETED and removes the stored bytes, so no
future recognition package can contain the person. Templates are never returned by this
module to a dashboard caller; only the admin package builder reads them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission
from veotrex_api.face_backend import MIN_FACE_SIZE_PX, FaceBackendError, FaceEnrollmentBackend
from veotrex_api.models import AuditEvent, StaffEnrollmentImage, StaffFaceTemplate, StaffProfile
from veotrex_api.staff_media import (
    EnrollmentImageRejected,
    StaffMediaStore,
    validate_enrollment_image,
)

MIN_ACCEPTED_IMAGES = 3
MAX_ACCEPTED_IMAGES = 5
MAX_STAFF_PER_TENANT = 500
DISPLAY_NAME_MAX = 200

# Every rejection a caller can receive. Bounded, human-readable, no model internals.
REJECTION_CATEGORIES = frozenset(
    {
        "empty_upload",
        "file_too_large",
        "unsupported_type",
        "invalid_image",
        "image_too_large",
        "image_too_small",
        "no_face_detected",
        "multiple_faces",
        "face_too_small",
        "duplicate_image",
        "enrollment_limit_reached",
        "face_backend_unavailable",
        "template_failed",
        "profile_not_active",
    }
)


class StaffError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(f"staff operation failed: {category}")
        self.category = category


@dataclass(frozen=True, slots=True)
class StaffSummary:
    staff_id: UUID
    display_name: str
    status: str
    enrollment_state: str
    accepted_images: int
    required_images: int
    maximum_images: int
    recognition_ready: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EnrollmentImageSummary:
    image_id: UUID
    width: int
    height: int
    byte_size: int
    face_size_px: int | None
    quality: int | None
    template_state: str
    created_at: datetime


class StaffEnrollmentService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        media: StaffMediaStore,
        backend: FaceEnrollmentBackend,
        *,
        max_image_bytes: int,
    ) -> None:
        self._factory = factory
        self._media = media
        self._backend = backend
        self._max_image_bytes = max_image_bytes
        self._logger = structlog.get_logger()

    # ------------------------------------------------------------------------- helpers
    @staticmethod
    async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    @staticmethod
    def _require(principal: AuthenticatedPrincipal, permission: Permission) -> None:
        if permission not in principal.permissions:
            raise StaffError("access_denied")

    async def _profile(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        staff_id: UUID,
        *,
        for_update: bool = False,
        include_deleted: bool = False,
    ) -> StaffProfile:
        statement = select(StaffProfile).where(
            StaffProfile.id == staff_id, StaffProfile.tenant_id == tenant_id
        )
        if for_update:
            statement = statement.with_for_update()
        profile = await session.scalar(statement)
        if profile is None or (profile.status == "DELETED" and not include_deleted):
            # Unknown and other-tenant identifiers are indistinguishable.
            raise StaffError("not_found")
        return profile

    async def _accepted_count(self, session: AsyncSession, tenant_id: UUID, staff_id: UUID) -> int:
        value = await session.scalar(
            select(func.count())
            .select_from(StaffEnrollmentImage)
            .where(
                StaffEnrollmentImage.tenant_id == tenant_id,
                StaffEnrollmentImage.staff_profile_id == staff_id,
                StaffEnrollmentImage.status == "ACCEPTED",
            )
        )
        return int(value or 0)

    async def _recompute_state(self, session: AsyncSession, profile: StaffProfile) -> None:
        """Backend-authoritative readiness from persisted rows, never from a count alone."""
        accepted = (
            await session.scalars(
                select(StaffEnrollmentImage.id).where(
                    StaffEnrollmentImage.tenant_id == profile.tenant_id,
                    StaffEnrollmentImage.staff_profile_id == profile.id,
                    StaffEnrollmentImage.status == "ACCEPTED",
                )
            )
        ).all()
        if not accepted:
            profile.enrollment_state = "EMPTY"
            return
        templated = set(
            (
                await session.scalars(
                    select(StaffFaceTemplate.enrollment_image_id).where(
                        StaffFaceTemplate.tenant_id == profile.tenant_id,
                        StaffFaceTemplate.staff_profile_id == profile.id,
                        StaffFaceTemplate.status == "ACTIVE",
                        StaffFaceTemplate.model_id == self._backend.model_id,
                        StaffFaceTemplate.model_version == self._backend.model_version,
                        StaffFaceTemplate.template_version == self._backend.template_version,
                    )
                )
            ).all()
        )
        if any(image_id not in templated for image_id in accepted):
            profile.enrollment_state = "FAILED"
        elif len(accepted) >= MIN_ACCEPTED_IMAGES:
            profile.enrollment_state = "READY"
        else:
            profile.enrollment_state = "COLLECTING"

    @staticmethod
    def _audit(
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        staff_id: UUID,
        action: str,
        request_id: str,
        metadata: dict[str, str | int],
    ) -> None:
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_id=principal.actor_id,
                action=action,
                target_type="staff_profile",
                target_id=staff_id,
                request_id=request_id[:128],
                metadata_=metadata,
            )
        )

    async def _summary(self, session: AsyncSession, profile: StaffProfile) -> StaffSummary:
        accepted = await self._accepted_count(session, profile.tenant_id, profile.id)
        return StaffSummary(
            profile.id,
            profile.display_name,
            profile.status,
            profile.enrollment_state,
            accepted,
            MIN_ACCEPTED_IMAGES,
            MAX_ACCEPTED_IMAGES,
            profile.status == "ACTIVE" and profile.enrollment_state == "READY",
            profile.created_at,
            profile.updated_at,
        )

    @staticmethod
    def _clean_name(value: str) -> str:
        name = " ".join(value.split())
        if not name or len(name) > DISPLAY_NAME_MAX:
            raise StaffError("invalid_display_name")
        return name

    # ---------------------------------------------------------------------- profiles
    async def list_profiles(self, principal: AuthenticatedPrincipal) -> tuple[StaffSummary, ...]:
        self._require(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            profiles = (
                await session.scalars(
                    select(StaffProfile)
                    .where(
                        StaffProfile.tenant_id == principal.tenant_id,
                        StaffProfile.status != "DELETED",
                    )
                    .order_by(StaffProfile.display_name, StaffProfile.id)
                )
            ).all()
            return tuple([await self._summary(session, profile) for profile in profiles])

    async def get_profile(self, principal: AuthenticatedPrincipal, staff_id: UUID) -> StaffSummary:
        self._require(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            profile = await self._profile(session, principal.tenant_id, staff_id)
            return await self._summary(session, profile)

    async def create_profile(
        self, principal: AuthenticatedPrincipal, display_name: str, request_id: str
    ) -> StaffSummary:
        self._require(principal, Permission.MANAGE_STAFF)
        name = self._clean_name(display_name)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            existing = await session.scalar(
                select(func.count())
                .select_from(StaffProfile)
                .where(
                    StaffProfile.tenant_id == principal.tenant_id,
                    StaffProfile.status != "DELETED",
                )
            )
            if int(existing or 0) >= MAX_STAFF_PER_TENANT:
                raise StaffError("staff_limit_reached")
            profile = StaffProfile(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                display_name=name,
                status="ACTIVE",
                enrollment_state="EMPTY",
                created_by_actor_id=principal.actor_id,
            )
            session.add(profile)
            await session.flush()
            self._audit(session, principal, profile.id, "staff.created", request_id, {})
            await session.refresh(profile)
            return await self._summary(session, profile)

    async def rename_profile(
        self,
        principal: AuthenticatedPrincipal,
        staff_id: UUID,
        display_name: str,
        request_id: str,
    ) -> StaffSummary:
        self._require(principal, Permission.MANAGE_STAFF)
        name = self._clean_name(display_name)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            profile = await self._profile(session, principal.tenant_id, staff_id, for_update=True)
            profile.display_name = name
            self._audit(session, principal, staff_id, "staff.renamed", request_id, {})
            await session.flush()
            await session.refresh(profile)
            return await self._summary(session, profile)

    async def set_active(
        self, principal: AuthenticatedPrincipal, staff_id: UUID, active: bool, request_id: str
    ) -> StaffSummary:
        self._require(principal, Permission.MANAGE_STAFF)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            profile = await self._profile(session, principal.tenant_id, staff_id, for_update=True)
            if active:
                profile.status = "ACTIVE"
                profile.deactivated_at = None
                action = "staff.activated"
            else:
                profile.status = "INACTIVE"
                profile.deactivated_at = datetime.now(UTC)
                action = "staff.deactivated"
            await self._recompute_state(session, profile)
            self._audit(
                session, principal, staff_id, action, request_id, {"to_status": profile.status}
            )
            await session.flush()
            await session.refresh(profile)
            return await self._summary(session, profile)

    async def delete_profile(
        self, principal: AuthenticatedPrincipal, staff_id: UUID, request_id: str
    ) -> None:
        """Soft-delete the profile, revoke every template, delete every image and its bytes."""
        self._require(principal, Permission.MANAGE_STAFF)
        keys: list[str] = []
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            profile = await self._profile(session, principal.tenant_id, staff_id, for_update=True)
            now = datetime.now(UTC)
            templates = (
                await session.scalars(
                    select(StaffFaceTemplate).where(
                        StaffFaceTemplate.tenant_id == principal.tenant_id,
                        StaffFaceTemplate.staff_profile_id == staff_id,
                        StaffFaceTemplate.status == "ACTIVE",
                    )
                )
            ).all()
            for template in templates:
                template.status = "REVOKED"
                template.revoked_at = now
            images = (
                await session.scalars(
                    select(StaffEnrollmentImage).where(
                        StaffEnrollmentImage.tenant_id == principal.tenant_id,
                        StaffEnrollmentImage.staff_profile_id == staff_id,
                        StaffEnrollmentImage.status == "ACCEPTED",
                    )
                )
            ).all()
            for image in images:
                if image.media_key:
                    keys.append(image.media_key)
                image.status = "DELETED"
                image.deleted_at = now
                image.media_key = None
            profile.status = "DELETED"
            profile.deleted_at = now
            profile.enrollment_state = "EMPTY"
            self._audit(
                session,
                principal,
                staff_id,
                "staff.deleted",
                request_id,
                {"templates_revoked": len(templates), "images_deleted": len(images)},
            )
        # Bytes go after the commit: a failed commit must not orphan-delete files, and a file
        # that lingers after a committed delete is unreachable (its key is gone from the row).
        for key in keys:
            self._media.delete(principal.tenant_id, key)

    # ------------------------------------------------------------------------ images
    async def list_images(
        self, principal: AuthenticatedPrincipal, staff_id: UUID
    ) -> tuple[EnrollmentImageSummary, ...]:
        self._require(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            await self._profile(session, principal.tenant_id, staff_id)
            images = (
                await session.scalars(
                    select(StaffEnrollmentImage)
                    .where(
                        StaffEnrollmentImage.tenant_id == principal.tenant_id,
                        StaffEnrollmentImage.staff_profile_id == staff_id,
                        StaffEnrollmentImage.status == "ACCEPTED",
                    )
                    .order_by(StaffEnrollmentImage.created_at, StaffEnrollmentImage.id)
                )
            ).all()
            templated = set(
                (
                    await session.scalars(
                        select(StaffFaceTemplate.enrollment_image_id).where(
                            StaffFaceTemplate.tenant_id == principal.tenant_id,
                            StaffFaceTemplate.staff_profile_id == staff_id,
                            StaffFaceTemplate.status == "ACTIVE",
                            StaffFaceTemplate.model_id == self._backend.model_id,
                            StaffFaceTemplate.model_version == self._backend.model_version,
                        )
                    )
                ).all()
            )
        return tuple(
            EnrollmentImageSummary(
                image.id,
                image.width,
                image.height,
                image.byte_size,
                image.face_size_px,
                image.quality,
                "READY" if image.id in templated else "MISSING",
                image.created_at,
            )
            for image in images
        )

    async def add_image(
        self, principal: AuthenticatedPrincipal, staff_id: UUID, data: bytes, request_id: str
    ) -> EnrollmentImageSummary:
        """Validate, analyse, store and template one photo, or reject it with a category."""
        self._require(principal, Permission.MANAGE_STAFF)
        validated = validate_enrollment_image(data, max_bytes=self._max_image_bytes)
        try:
            observation = self._backend.analyze(validated.image)
        except FaceBackendError as exc:
            raise EnrollmentImageRejected(exc.category) from None
        if observation.face_count == 0:
            raise EnrollmentImageRejected("no_face_detected")
        if observation.face_count > 1:
            raise EnrollmentImageRejected("multiple_faces")
        if observation.face_size_px is not None and observation.face_size_px < MIN_FACE_SIZE_PX:
            raise EnrollmentImageRejected("face_too_small")
        try:
            template = self._backend.extract_template(validated.image)
        except FaceBackendError:
            raise EnrollmentImageRejected("template_failed") from None

        stored_key: str | None = None
        try:
            async with self._factory() as session, session.begin():
                await self._set_tenant(session, principal.tenant_id)
                profile = await self._profile(
                    session, principal.tenant_id, staff_id, for_update=True
                )
                if profile.status != "ACTIVE":
                    raise EnrollmentImageRejected("profile_not_active")
                if (
                    await self._accepted_count(session, principal.tenant_id, staff_id)
                    >= MAX_ACCEPTED_IMAGES
                ):
                    raise EnrollmentImageRejected("enrollment_limit_reached")
                duplicate = await session.scalar(
                    select(func.count())
                    .select_from(StaffEnrollmentImage)
                    .where(
                        StaffEnrollmentImage.tenant_id == principal.tenant_id,
                        StaffEnrollmentImage.staff_profile_id == staff_id,
                        StaffEnrollmentImage.status == "ACCEPTED",
                        StaffEnrollmentImage.content_sha256 == validated.content_sha256,
                    )
                )
                if int(duplicate or 0):
                    raise EnrollmentImageRejected("duplicate_image")
                profile.enrollment_state = "PROCESSING"
                stored_key = self._media.put(principal.tenant_id, validated.canonical_bytes)
                image = StaffEnrollmentImage(
                    id=uuid4(),
                    tenant_id=principal.tenant_id,
                    staff_profile_id=staff_id,
                    media_key=stored_key,
                    media_type="image/jpeg",
                    content_sha256=validated.content_sha256,
                    width=validated.width,
                    height=validated.height,
                    byte_size=len(validated.canonical_bytes),
                    face_size_px=observation.face_size_px,
                    quality=observation.quality,
                    status="ACCEPTED",
                )
                session.add(image)
                await session.flush()
                session.add(
                    StaffFaceTemplate(
                        id=uuid4(),
                        tenant_id=principal.tenant_id,
                        staff_profile_id=staff_id,
                        enrollment_image_id=image.id,
                        model_id=template.model_id,
                        model_version=template.model_version,
                        template_version=template.template_version,
                        dimensions=template.dimensions,
                        dtype=template.dtype,
                        template=template.data,
                        quality=template.quality,
                        status="ACTIVE",
                    )
                )
                await session.flush()
                await self._recompute_state(session, profile)
                self._audit(
                    session,
                    principal,
                    staff_id,
                    "staff.image_accepted",
                    request_id,
                    {"enrollment_state": profile.enrollment_state},
                )
                await session.refresh(image)
                summary = EnrollmentImageSummary(
                    image.id,
                    image.width,
                    image.height,
                    image.byte_size,
                    image.face_size_px,
                    image.quality,
                    "READY",
                    image.created_at,
                )
        except IntegrityError:
            if stored_key:
                self._media.delete(principal.tenant_id, stored_key)
            raise EnrollmentImageRejected("duplicate_image") from None
        except BaseException:
            if stored_key:
                self._media.delete(principal.tenant_id, stored_key)
            raise
        self._logger.info(
            "staff_enrollment_image_accepted",
            width=summary.width,
            height=summary.height,
            byte_size=summary.byte_size,
        )
        return summary

    async def image_content(
        self, principal: AuthenticatedPrincipal, staff_id: UUID, image_id: UUID
    ) -> bytes:
        self._require(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            await self._profile(session, principal.tenant_id, staff_id)
            image = await session.scalar(
                select(StaffEnrollmentImage).where(
                    StaffEnrollmentImage.id == image_id,
                    StaffEnrollmentImage.tenant_id == principal.tenant_id,
                    StaffEnrollmentImage.staff_profile_id == staff_id,
                    StaffEnrollmentImage.status == "ACCEPTED",
                )
            )
            if image is None or image.media_key is None:
                raise StaffError("not_found")
            key = image.media_key
        data = self._media.get(principal.tenant_id, key)
        if data is None:
            raise StaffError("not_found")
        return data

    async def remove_image(
        self, principal: AuthenticatedPrincipal, staff_id: UUID, image_id: UUID, request_id: str
    ) -> StaffSummary:
        self._require(principal, Permission.MANAGE_STAFF)
        key: str | None = None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            profile = await self._profile(session, principal.tenant_id, staff_id, for_update=True)
            image = await session.scalar(
                select(StaffEnrollmentImage)
                .where(
                    StaffEnrollmentImage.id == image_id,
                    StaffEnrollmentImage.tenant_id == principal.tenant_id,
                    StaffEnrollmentImage.staff_profile_id == staff_id,
                    StaffEnrollmentImage.status == "ACCEPTED",
                )
                .with_for_update()
            )
            if image is None:
                raise StaffError("not_found")
            now = datetime.now(UTC)
            key = image.media_key
            image.status = "DELETED"
            image.deleted_at = now
            image.media_key = None
            templates = (
                await session.scalars(
                    select(StaffFaceTemplate).where(
                        StaffFaceTemplate.tenant_id == principal.tenant_id,
                        StaffFaceTemplate.enrollment_image_id == image_id,
                        StaffFaceTemplate.status == "ACTIVE",
                    )
                )
            ).all()
            for template in templates:
                template.status = "REVOKED"
                template.revoked_at = now
            await self._recompute_state(session, profile)
            self._audit(
                session,
                principal,
                staff_id,
                "staff.image_removed",
                request_id,
                {"enrollment_state": profile.enrollment_state},
            )
            await session.flush()
            await session.refresh(profile)
            summary = await self._summary(session, profile)
        if key:
            self._media.delete(principal.tenant_id, key)
        return summary
