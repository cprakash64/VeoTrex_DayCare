"""Camera portal (doorway line) configuration (V1-05A).

Built on :class:`~veotrex_api.classroom_service.ClassroomService`: the caller's tenant (RLS
context plus explicit ``tenant_id`` predicates), one facility at a time (READ_OPERATIONAL to
read, CONFIGURE_FACILITY_CAMERAS there to change anything), and an unknown, other-tenant,
unreadable or not-in-this-classroom camera answered identically as ``not_found``.

A portal is geometry an operator configures for one camera in one classroom. It says nothing
about people. V1-05B (ADR 0030) adds ``GET /v1/edge/runtime-config``, from which an edge node
running in managed mode pulls the ACTIVE portals of its own assigned cameras. No deployed node
consumes it yet, so distribution is still reported as ``NOT_CONNECTED`` and each portal is also
rendered as the exact ``veotrex-edge live-demo --portal`` text for local evaluation (ADR 0029).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission
from veotrex_api.camera_portal import (
    DEFAULT_DEADBAND,
    MAX_PORTALS,
    PortalConfigError,
    PortalGeometry,
    clean_label,
    edge_flag,
    validate_geometry,
)
from veotrex_api.classroom_service import ClassroomError, ClassroomService, utc
from veotrex_api.models import Area, Camera, CameraPortal, Facility, Zone

PORTAL_VALIDATION_CATEGORIES = frozenset(
    {
        "invalid_portal_label",
        "invalid_portal_coordinates",
        "invalid_portal_inside",
        "invalid_portal_deadband",
        "portal_too_short",
        "portal_inside_ambiguous",
    }
)
PORTAL_CONFLICT_CATEGORIES = frozenset(
    {"portal_limit_reached", "portal_label_exists", "portal_archived", "camera_inactive"}
)
# Said on every response so no screen can imply that saving a portal changed what a camera
# reports. V1-05B implemented the managed edge path (/v1/edge/runtime-config) but no edge node
# is deployed consuming it, so this stays NOT_CONNECTED until one is (ADR 0030).
EDGE_DISTRIBUTION = "NOT_CONNECTED"


@dataclass(frozen=True, slots=True)
class PortalInput:
    label: str
    x1: float
    y1: float
    x2: float
    y2: float
    inside_side: str
    deadband: float = DEFAULT_DEADBAND
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class PortalChange:
    label: str | None = None
    x1: float | None = None
    y1: float | None = None
    x2: float | None = None
    y2: float | None = None
    inside_side: str | None = None
    deadband: float | None = None
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class PortalSummary:
    portal_id: UUID
    label: str
    x1: float
    y1: float
    x2: float
    y2: float
    inside_side: str
    inside_normal: tuple[float, float]
    deadband: float
    enabled: bool
    status: str
    revision: int
    edge_flag: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class CameraPortals:
    classroom_id: UUID
    classroom_name: str
    camera_id: UUID
    camera_name: str
    camera_status: str
    can_configure: bool
    max_portals: int
    edge_distribution: str
    portals: tuple[PortalSummary, ...]


def _clean(error: PortalConfigError) -> ClassroomError:
    return ClassroomError(error.category)


def _geometry(row: CameraPortal) -> PortalGeometry:
    return validate_geometry(row.x1, row.y1, row.x2, row.y2, row.inside_side, row.deadband)


class CameraPortalService(ClassroomService):
    async def _camera(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        camera_id: UUID,
        *,
        configure: bool = False,
    ) -> tuple[Area, Facility, Camera]:
        area, facility = await self._classroom(session, principal, classroom_id)
        camera = await session.scalar(
            select(Camera)
            .join(Zone, (Zone.id == Camera.zone_id) & (Zone.tenant_id == Camera.tenant_id))
            .where(
                Camera.id == camera_id,
                Camera.tenant_id == principal.tenant_id,
                Zone.area_id == area.id,
                Camera.status != "ARCHIVED",
            )
        )
        if camera is None:
            # Unknown, other-tenant and other-classroom cameras are indistinguishable.
            raise ClassroomError("not_found")
        if configure and not self._can(
            principal, Permission.CONFIGURE_FACILITY_CAMERAS, facility.id
        ):
            raise ClassroomError("access_denied")
        return area, facility, camera

    @staticmethod
    async def _lock_camera(session: AsyncSession, tenant_id: UUID, camera_id: UUID) -> None:
        """Serialise portal writes per camera, so two creators cannot both pass the count."""
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"camera_portals:{tenant_id}:{camera_id}"},
        )

    @staticmethod
    def _portal_summary(row: CameraPortal) -> PortalSummary:
        geometry = _geometry(row)
        nx, ny = geometry.inside_normal()
        return PortalSummary(
            portal_id=row.id,
            label=row.label,
            x1=row.x1,
            y1=row.y1,
            x2=row.x2,
            y2=row.y2,
            inside_side=row.inside_side,
            inside_normal=(round(nx, 4), round(ny, 4)),
            deadband=row.deadband,
            enabled=row.enabled,
            status=row.status,
            revision=row.revision,
            edge_flag=edge_flag(str(row.id), geometry, row.label),
            created_at=utc(row.created_at),
            updated_at=utc(row.updated_at),
            archived_at=None if row.archived_at is None else utc(row.archived_at),
        )

    @staticmethod
    def _audit_terms(row: CameraPortal) -> dict[str, Any]:
        """Geometry and flags only; the operator's label is named, never copied."""
        return {
            "x1": row.x1,
            "y1": row.y1,
            "x2": row.x2,
            "y2": row.y2,
            "inside_side": row.inside_side,
            "deadband": row.deadband,
            "enabled": row.enabled,
            "status": row.status,
            "revision": row.revision,
        }

    async def _view(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        area: Area,
        facility: Facility,
        camera: Camera,
    ) -> CameraPortals:
        rows = (
            await session.scalars(
                select(CameraPortal)
                .where(
                    CameraPortal.tenant_id == principal.tenant_id,
                    CameraPortal.camera_id == camera.id,
                    CameraPortal.area_id == area.id,
                )
                .order_by(
                    CameraPortal.status,
                    CameraPortal.created_at,
                    CameraPortal.id,
                )
            )
        ).all()
        return CameraPortals(
            classroom_id=area.id,
            classroom_name=area.name,
            camera_id=camera.id,
            camera_name=camera.name,
            camera_status=camera.status,
            can_configure=self._can(principal, Permission.CONFIGURE_FACILITY_CAMERAS, facility.id),
            max_portals=MAX_PORTALS,
            edge_distribution=EDGE_DISTRIBUTION,
            portals=tuple(self._portal_summary(row) for row in rows),
        )

    async def list_portals(
        self, principal: AuthenticatedPrincipal, classroom_id: UUID, camera_id: UUID
    ) -> CameraPortals:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility, camera = await self._camera(session, principal, classroom_id, camera_id)
            return await self._view(session, principal, area, facility, camera)

    async def create_portal(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        camera_id: UUID,
        payload: PortalInput,
        request_id: str,
    ) -> CameraPortals:
        self._require_any(principal, Permission.CONFIGURE_FACILITY_CAMERAS)
        try:
            label = clean_label(payload.label)
            geometry = validate_geometry(
                payload.x1,
                payload.y1,
                payload.x2,
                payload.y2,
                payload.inside_side,
                payload.deadband,
            )
        except PortalConfigError as exc:
            raise _clean(exc) from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility, camera = await self._camera(
                session, principal, classroom_id, camera_id, configure=True
            )
            if camera.status != "ACTIVE" or area.status != "ACTIVE":
                raise ClassroomError("camera_inactive")
            await self._lock_camera(session, principal.tenant_id, camera.id)
            active = await session.scalar(
                select(func.count())
                .select_from(CameraPortal)
                .where(
                    CameraPortal.tenant_id == principal.tenant_id,
                    CameraPortal.camera_id == camera.id,
                    CameraPortal.status == "ACTIVE",
                )
            )
            if int(active or 0) >= MAX_PORTALS:
                raise ClassroomError("portal_limit_reached")
            row = CameraPortal(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                facility_id=facility.id,
                area_id=area.id,
                camera_id=camera.id,
                label=label,
                x1=geometry.x1,
                y1=geometry.y1,
                x2=geometry.x2,
                y2=geometry.y2,
                inside_side=str(geometry.inside),
                deadband=geometry.deadband,
                enabled=bool(payload.enabled),
                status="ACTIVE",
                revision=1,
                created_by_actor_id=principal.actor_id,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("portal_label_exists") from None
            self._audit(
                session,
                principal,
                "camera_portal",
                row.id,
                "camera_portal.created",
                request_id,
                {
                    "facility_id": str(facility.id),
                    "classroom_id": str(area.id),
                    "camera_id": str(camera.id),
                    **self._audit_terms(row),
                },
            )
            await session.flush()
            return await self._view(session, principal, area, facility, camera)

    async def _portal_row(
        self, session: AsyncSession, tenant_id: UUID, camera: Camera, area: Area, portal_id: UUID
    ) -> CameraPortal:
        row = await session.scalar(
            select(CameraPortal)
            .where(
                CameraPortal.id == portal_id,
                CameraPortal.tenant_id == tenant_id,
                CameraPortal.camera_id == camera.id,
                CameraPortal.area_id == area.id,
            )
            .with_for_update()
        )
        if row is None:
            raise ClassroomError("not_found")
        return row

    async def update_portal(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        camera_id: UUID,
        portal_id: UUID,
        change: PortalChange,
        request_id: str,
    ) -> CameraPortals:
        """Edit an ACTIVE portal in place (revision + 1, bounded before/after audited)."""
        self._require_any(principal, Permission.CONFIGURE_FACILITY_CAMERAS)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility, camera = await self._camera(
                session, principal, classroom_id, camera_id, configure=True
            )
            await self._lock_camera(session, principal.tenant_id, camera.id)
            row = await self._portal_row(session, principal.tenant_id, camera, area, portal_id)
            if row.status != "ACTIVE":
                raise ClassroomError("portal_archived")
            try:
                label = row.label if change.label is None else clean_label(change.label)
                geometry = validate_geometry(
                    row.x1 if change.x1 is None else change.x1,
                    row.y1 if change.y1 is None else change.y1,
                    row.x2 if change.x2 is None else change.x2,
                    row.y2 if change.y2 is None else change.y2,
                    row.inside_side if change.inside_side is None else change.inside_side,
                    row.deadband if change.deadband is None else change.deadband,
                )
            except PortalConfigError as exc:
                raise _clean(exc) from None
            before = self._audit_terms(row)
            changed: list[str] = []
            updates: dict[str, Any] = {
                "label": label,
                "x1": geometry.x1,
                "y1": geometry.y1,
                "x2": geometry.x2,
                "y2": geometry.y2,
                "inside_side": str(geometry.inside),
                "deadband": geometry.deadband,
                "enabled": row.enabled if change.enabled is None else bool(change.enabled),
            }
            for name, value in updates.items():
                if getattr(row, name) != value:
                    setattr(row, name, value)
                    changed.append(name)
            if changed:
                row.revision += 1
                try:
                    await session.flush()
                except IntegrityError:
                    raise ClassroomError("portal_label_exists") from None
                self._audit(
                    session,
                    principal,
                    "camera_portal",
                    row.id,
                    "camera_portal.updated",
                    request_id,
                    {
                        "facility_id": str(facility.id),
                        "classroom_id": str(area.id),
                        "camera_id": str(camera.id),
                        "changed_fields": changed,
                        "before": before,
                        "after": self._audit_terms(row),
                    },
                )
                await session.flush()
            return await self._view(session, principal, area, facility, camera)

    async def archive_portal(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        camera_id: UUID,
        portal_id: UUID,
        request_id: str,
    ) -> CameraPortals:
        """Retire a portal. Terminal and idempotent; the row is kept, never deleted."""
        self._require_any(principal, Permission.CONFIGURE_FACILITY_CAMERAS)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility, camera = await self._camera(
                session, principal, classroom_id, camera_id, configure=True
            )
            await self._lock_camera(session, principal.tenant_id, camera.id)
            row = await self._portal_row(session, principal.tenant_id, camera, area, portal_id)
            if row.status == "ACTIVE":
                before = self._audit_terms(row)
                row.status = "ARCHIVED"
                row.enabled = False
                row.revision += 1
                row.archived_at = datetime.now(UTC)
                row.archived_by_actor_id = principal.actor_id
                await session.flush()
                self._audit(
                    session,
                    principal,
                    "camera_portal",
                    row.id,
                    "camera_portal.archived",
                    request_id,
                    {
                        "facility_id": str(facility.id),
                        "classroom_id": str(area.id),
                        "camera_id": str(camera.id),
                        "before": before,
                        "after": self._audit_terms(row),
                    },
                )
                await session.flush()
            return await self._view(session, principal, area, facility, camera)
