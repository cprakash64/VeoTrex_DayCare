"""Edge runtime configuration and anonymous room-transition ingest (V1-05B).

Two narrow machine-authenticated operations, used only through ``/v1/edge/...``:

``runtime_config``
    The portals of the cameras actively assigned to *the authenticated node* - nothing else. The
    node, tenant and facility come from the credential (``EdgePrincipal``); a query parameter or
    body can never name another node. Each camera carries a deterministic
    ``configuration_revision``: SHA-256 over a canonical JSON document of its assignment and its
    ACTIVE portals (enabled or not), so the same configuration always yields the same revision
    and any edit, enable/disable or archive yields a new one. The edge recomputes the same hash
    to validate what it received (and what it cached). No provider id, Ring token, name, roster,
    attendance, policy or face setting is ever included.

``ingest``
    A batch of anonymous ``PERSON_ENTERED_ROOM`` / ``PERSON_EXITED_ROOM`` events. Each is checked
    against server-side state only: the camera must be actively assigned to this node; the
    portal must belong to that camera and to the classroom the camera is in; the time must be
    inside the accepted window. Tenant, facility and classroom are taken from those rows. The
    edge-generated ``event_id`` is the primary key, so a retry of the same event is reported as
    a duplicate rather than inserted twice. Every refusal is a bounded category; "not yours" and
    "does not exist" are indistinguishable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.edge_auth import EdgePrincipal
from veotrex_api.models import (
    Area,
    Camera,
    CameraAssignment,
    CameraPortal,
    EdgeNode,
    RoomTransitionEvent,
    Zone,
)

RUNTIME_CONFIG_SCHEMA_VERSION = 1
USABLE_CAMERA_STATES = ("DISCOVERED", "ACTIVE")
# One node serves a handful of cameras; the bound keeps one response small whatever the data.
MAX_CAMERAS_PER_NODE = 32
MAX_EVENTS_PER_BATCH = 100
# An event may wait in the edge outbox through an outage of up to this long.
MAX_EVENT_AGE = timedelta(days=7)
MAX_EVENT_FUTURE_SKEW = timedelta(seconds=120)
EVENT_TYPES = {"PERSON_ENTERED_ROOM": "ENTERED", "PERSON_EXITED_ROOM": "EXITED"}


class EdgeRuntimeUnavailable(Exception):
    """The database could not be reached. Fails closed as 503."""


def canonical_json(document: Any) -> bytes:
    """Deterministic serialisation: sorted keys, no whitespace, ASCII, shortest float repr."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def configuration_revision(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(document)).hexdigest()


def portal_document(row: CameraPortal) -> dict[str, Any]:
    return {
        "portal_id": str(row.id),
        "label": row.label,
        "x1": float(row.x1),
        "y1": float(row.y1),
        "x2": float(row.x2),
        "y2": float(row.y2),
        "inside": row.inside_side,
        "enabled": bool(row.enabled),
        "deadband": float(row.deadband),
        "revision": int(row.revision),
    }


def camera_document(
    camera_id: UUID, assignment_id: UUID, portals: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """The exact document a camera's revision is computed over (the edge does the same)."""
    return {
        "camera_id": str(camera_id),
        "assignment_id": str(assignment_id),
        "portals": sorted(portals, key=lambda item: str(item["portal_id"])),
    }


def snapshot_version(cameras: Sequence[dict[str, Any]]) -> str:
    return configuration_revision(
        {
            "cameras": sorted(
                [
                    {"camera_id": c["camera_id"], "revision": c["configuration_revision"]}
                    for c in cameras
                ],
                key=lambda item: item["camera_id"],
            )
        }
    )


@dataclass(frozen=True, slots=True)
class RoomTransitionInput:
    event_id: UUID
    camera_id: UUID
    portal_id: UUID
    event_type: str  # PERSON_ENTERED_ROOM / PERSON_EXITED_ROOM
    occurred_at: datetime
    ephemeral_track_id: int
    stream_instance_id: str
    crossing_x: float
    crossing_y: float
    evidence_observations: int


@dataclass(frozen=True, slots=True)
class IngestResult:
    event_id: UUID
    status: str  # ACCEPTED / DUPLICATE / REJECTED
    category: str | None = None


@dataclass(frozen=True, slots=True)
class _AssignedCamera:
    camera_id: UUID
    assignment_id: UUID
    area_id: UUID | None
    facility_id: UUID | None


async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


class EdgeRuntimeService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    @staticmethod
    async def _assigned(
        session: AsyncSession, principal: EdgePrincipal
    ) -> dict[UUID, _AssignedCamera]:
        """Cameras actively assigned to exactly this node, with the classroom each is in.

        A camera whose classroom is in another facility than the node's own is returned without
        a classroom: it keeps its (empty) place in the config, but no portal is sent for it and
        no event is accepted from it.
        """
        rows = (
            await session.execute(
                select(
                    Camera.id,
                    CameraAssignment.id.label("assignment_id"),
                    Area.id.label("area_id"),
                    Area.facility_id,
                )
                .select_from(CameraAssignment)
                .join(
                    EdgeNode,
                    and_(
                        EdgeNode.id == CameraAssignment.edge_node_id,
                        EdgeNode.tenant_id == CameraAssignment.tenant_id,
                    ),
                )
                .join(
                    Camera,
                    and_(
                        Camera.id == CameraAssignment.camera_id,
                        Camera.tenant_id == CameraAssignment.tenant_id,
                    ),
                )
                .outerjoin(
                    Zone, and_(Zone.id == Camera.zone_id, Zone.tenant_id == Camera.tenant_id)
                )
                .outerjoin(Area, and_(Area.id == Zone.area_id, Area.tenant_id == Zone.tenant_id))
                .where(
                    CameraAssignment.tenant_id == principal.tenant_id,
                    CameraAssignment.edge_node_id == principal.edge_node_id,
                    CameraAssignment.ended_at.is_(None),
                    EdgeNode.status != "DISABLED",
                    Camera.status.in_(USABLE_CAMERA_STATES),
                )
                .order_by(Camera.id)
                .limit(MAX_CAMERAS_PER_NODE)
            )
        ).all()
        cameras: dict[UUID, _AssignedCamera] = {}
        for row in rows:
            placed = row.area_id is not None and row.facility_id == principal.facility_id
            cameras[row.id] = _AssignedCamera(
                row.id,
                row.assignment_id,
                row.area_id if placed else None,
                row.facility_id if placed else None,
            )
        return cameras

    async def runtime_config(self, principal: EdgePrincipal) -> dict[str, Any]:
        try:
            async with self._factory() as session, session.begin():
                await _set_tenant(session, principal.tenant_id)
                assigned = await self._assigned(session, principal)
                portals: dict[UUID, list[dict[str, Any]]] = {camera: [] for camera in assigned}
                if assigned:
                    rows = (
                        await session.scalars(
                            select(CameraPortal).where(
                                CameraPortal.tenant_id == principal.tenant_id,
                                CameraPortal.camera_id.in_(list(assigned)),
                                CameraPortal.status == "ACTIVE",
                            )
                        )
                    ).all()
                    for row in rows:
                        camera = assigned[row.camera_id]
                        # A portal drawn for the camera's previous classroom does not apply.
                        if camera.area_id is not None and row.area_id == camera.area_id:
                            portals[row.camera_id].append(portal_document(row))
        except SQLAlchemyError:
            raise EdgeRuntimeUnavailable from None
        cameras = []
        for camera in assigned.values():
            document = camera_document(
                camera.camera_id, camera.assignment_id, portals[camera.camera_id]
            )
            cameras.append(
                {
                    **document,
                    "configuration_revision": configuration_revision(document),
                }
            )
        return {
            "schema_version": RUNTIME_CONFIG_SCHEMA_VERSION,
            "edge_node_id": str(principal.edge_node_id),
            "config_version": snapshot_version(cameras),
            "cameras": cameras,
        }

    async def ingest(
        self,
        principal: EdgePrincipal,
        events: Sequence[RoomTransitionInput],
        *,
        now: datetime | None = None,
    ) -> list[IngestResult]:
        current = now or datetime.now(UTC)
        results: list[IngestResult] = []
        try:
            async with self._factory() as session, session.begin():
                await _set_tenant(session, principal.tenant_id)
                assigned = await self._assigned(session, principal)
                wanted = {event.portal_id for event in events if event.camera_id in assigned}
                portals = (
                    {
                        row.id: row
                        for row in (
                            await session.scalars(
                                select(CameraPortal).where(
                                    CameraPortal.tenant_id == principal.tenant_id,
                                    CameraPortal.id.in_(list(wanted)),
                                )
                            )
                        ).all()
                    }
                    if wanted
                    else {}
                )
                for event in events:
                    results.append(
                        await self._one(session, principal, event, assigned, portals, current)
                    )
        except SQLAlchemyError:
            raise EdgeRuntimeUnavailable from None
        return results

    @staticmethod
    async def _one(
        session: AsyncSession,
        principal: EdgePrincipal,
        event: RoomTransitionInput,
        assigned: dict[UUID, _AssignedCamera],
        portals: dict[UUID, CameraPortal],
        now: datetime,
    ) -> IngestResult:
        camera = assigned.get(event.camera_id)
        if camera is None or camera.area_id is None or camera.facility_id is None:
            # Unknown, another node's, another tenant's, unassigned and unplaced cameras alike.
            return IngestResult(event.event_id, "REJECTED", "camera_unavailable")
        portal = portals.get(event.portal_id)
        if (
            portal is None
            or portal.camera_id != camera.camera_id
            or portal.area_id != camera.area_id
        ):
            return IngestResult(event.event_id, "REJECTED", "portal_unavailable")
        if not now - MAX_EVENT_AGE <= event.occurred_at <= now + MAX_EVENT_FUTURE_SKEW:
            return IngestResult(event.event_id, "REJECTED", "occurred_at_out_of_window")
        values = {
            "id": event.event_id,
            "tenant_id": principal.tenant_id,
            "facility_id": portal.facility_id,
            "area_id": portal.area_id,
            "camera_id": camera.camera_id,
            "edge_node_id": principal.edge_node_id,
            "portal_id": portal.id,
            "event_type": EVENT_TYPES[event.event_type],
            "occurred_at": event.occurred_at,
            "ephemeral_track_id": event.ephemeral_track_id,
            "stream_instance_id": event.stream_instance_id,
            "crossing_x": event.crossing_x,
            "crossing_y": event.crossing_y,
            "evidence_observations": event.evidence_observations,
        }
        try:
            async with session.begin_nested():
                inserted = await session.scalar(
                    insert(RoomTransitionEvent)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["id"])
                    .returning(RoomTransitionEvent.id)
                )
        except IntegrityError:
            # A table CHECK refused it (the savepoint is rolled back); the rest of the batch
            # proceeds. Everything checked here is also checked above, so this is a backstop.
            return IngestResult(event.event_id, "REJECTED", "invalid_event")
        if inserted is not None:
            return IngestResult(event.event_id, "ACCEPTED")
        existing = await session.scalar(
            select(RoomTransitionEvent).where(
                RoomTransitionEvent.id == event.event_id,
                RoomTransitionEvent.tenant_id == principal.tenant_id,
            )
        )
        if (
            existing is not None
            and existing.edge_node_id == principal.edge_node_id
            and existing.camera_id == camera.camera_id
            and existing.portal_id == portal.id
            and existing.event_type == values["event_type"]
            and existing.occurred_at == event.occurred_at
            and existing.ephemeral_track_id == event.ephemeral_track_id
            and existing.stream_instance_id == event.stream_instance_id
        ):
            return IngestResult(event.event_id, "DUPLICATE")
        # The id is taken by a different event (or another tenant's, which is invisible here).
        return IngestResult(event.event_id, "REJECTED", "event_id_conflict")
