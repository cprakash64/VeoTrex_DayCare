"""Operator read access to anonymous room-transition events (V1-05B), registered by ``main``.

``GET /v1/classrooms/{classroom_id}/room-transitions`` - human (Auth0) route, read:operational,
classroom-scoped exactly like every other classroom read (uniform 404 for unknown, other-tenant
and unreadable classrooms). Keyset-paginated, newest first, at most ``MAX_LIMIT`` rows per page,
optional camera / portal / type filters. Each row says only that a person entered or exited via
a named doorway line, on which camera, when. The camera-session track number the edge reported
stays in the database: it is not an identity and the operator surface does not show it, so no
screen can pair an entry with an exit into "the same person". An edge machine credential cannot
open this route.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from veotrex_api.access import AuthenticatedPrincipal, PrincipalContext, require_permission
from veotrex_api.authorization import Permission
from veotrex_api.classroom_service import ClassroomError, ClassroomService, utc
from veotrex_api.models import Camera, CameraPortal, RoomTransitionEvent

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
require_read_operational = require_permission(Permission.READ_OPERATIONAL)


@dataclass(frozen=True, slots=True)
class TransitionRow:
    event_id: UUID
    event_type: str
    occurred_at: datetime
    received_at: datetime
    camera_id: UUID
    camera_name: str
    portal_id: UUID
    portal_label: str


@dataclass(frozen=True, slots=True)
class TransitionPage:
    classroom_id: UUID
    events: tuple[TransitionRow, ...]
    next_cursor: str | None


def encode_cursor(occurred_at: datetime, event_id: UUID) -> str:
    raw = f"{utc(occurred_at).isoformat()}|{event_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
        moment, _, identifier = raw.partition("|")
        parsed = datetime.fromisoformat(moment)
        if parsed.tzinfo is None:
            raise ValueError("naive")
        return parsed, UUID(identifier)
    except (ValueError, UnicodeDecodeError):
        raise ClassroomError("invalid_cursor") from None


class RoomTransitionService(ClassroomService):
    async def list_transitions(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        *,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
        camera_id: UUID | None = None,
        portal_id: UUID | None = None,
        event_type: str | None = None,
    ) -> TransitionPage:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        bounded = max(1, min(limit, MAX_LIMIT))
        after = None if cursor is None else decode_cursor(cursor)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, _ = await self._classroom(session, principal, classroom_id)
            rows = await self._rows(
                session,
                principal.tenant_id,
                area.id,
                bounded + 1,
                after,
                camera_id,
                portal_id,
                event_type,
            )
        page = rows[:bounded]
        next_cursor = (
            encode_cursor(page[-1].occurred_at, page[-1].event_id) if len(rows) > bounded else None
        )
        return TransitionPage(area.id, tuple(page), next_cursor)

    @staticmethod
    async def _rows(
        session: AsyncSession,
        tenant_id: UUID,
        area_id: UUID,
        limit: int,
        after: tuple[datetime, UUID] | None,
        camera_id: UUID | None,
        portal_id: UUID | None,
        event_type: str | None,
    ) -> list[TransitionRow]:
        statement = (
            select(RoomTransitionEvent, Camera.name, CameraPortal.label)
            .join(
                Camera,
                and_(
                    Camera.id == RoomTransitionEvent.camera_id,
                    Camera.tenant_id == RoomTransitionEvent.tenant_id,
                ),
            )
            .join(
                CameraPortal,
                and_(
                    CameraPortal.id == RoomTransitionEvent.portal_id,
                    CameraPortal.tenant_id == RoomTransitionEvent.tenant_id,
                ),
            )
            .where(
                RoomTransitionEvent.tenant_id == tenant_id,
                RoomTransitionEvent.area_id == area_id,
            )
        )
        if camera_id is not None:
            statement = statement.where(RoomTransitionEvent.camera_id == camera_id)
        if portal_id is not None:
            statement = statement.where(RoomTransitionEvent.portal_id == portal_id)
        if event_type is not None:
            statement = statement.where(RoomTransitionEvent.event_type == event_type)
        if after is not None:
            moment, identifier = after
            statement = statement.where(
                or_(
                    RoomTransitionEvent.occurred_at < moment,
                    and_(
                        RoomTransitionEvent.occurred_at == moment,
                        RoomTransitionEvent.id < identifier,
                    ),
                )
            )
        statement = statement.order_by(
            RoomTransitionEvent.occurred_at.desc(), RoomTransitionEvent.id.desc()
        ).limit(limit)
        return [
            TransitionRow(
                event_id=row[0].id,
                event_type=row[0].event_type,
                occurred_at=utc(row[0].occurred_at),
                received_at=utc(row[0].received_at),
                camera_id=row[0].camera_id,
                camera_name=row[1],
                portal_id=row[0].portal_id,
                portal_label=row[2],
            )
            for row in (await session.execute(statement)).all()
        ]


class RoomTransitionResponse(BaseModel):
    event_id: str
    event_type: str
    occurred_at: str
    received_at: str
    camera_id: str
    camera_name: str
    portal_id: str
    portal_label: str


class RoomTransitionPageResponse(BaseModel):
    classroom_id: str
    events: list[RoomTransitionResponse]
    next_cursor: str | None


def register_room_transition_routes(app: FastAPI, service: RoomTransitionService) -> None:
    @app.get(
        "/v1/classrooms/{classroom_id}/room-transitions",
        response_model=RoomTransitionPageResponse,
    )
    async def list_room_transitions(
        classroom_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=128, pattern=r"^[A-Za-z0-9_-]+$")] = None,
        camera_id: UUID | None = None,
        portal_id: UUID | None = None,
        event_type: Literal["ENTERED", "EXITED"] | None = None,
    ) -> RoomTransitionPageResponse:
        try:
            page = await service.list_transitions(
                context.principal,
                classroom_id,
                limit=limit,
                cursor=cursor,
                camera_id=camera_id,
                portal_id=portal_id,
                event_type=event_type,
            )
        except ClassroomError as exc:
            if exc.category == "not_found":
                raise HTTPException(status_code=404, detail="not found") from None
            if exc.category == "access_denied":
                raise HTTPException(status_code=403, detail="access denied") from None
            raise HTTPException(
                status_code=422,
                detail={"message": "room transition request rejected", "category": exc.category},
            ) from None
        return RoomTransitionPageResponse(
            classroom_id=str(page.classroom_id),
            events=[
                RoomTransitionResponse(
                    event_id=str(row.event_id),
                    event_type=row.event_type,
                    occurred_at=row.occurred_at.isoformat(),
                    received_at=row.received_at.isoformat(),
                    camera_id=str(row.camera_id),
                    camera_name=row.camera_name,
                    portal_id=str(row.portal_id),
                    portal_label=row.portal_label,
                )
                for row in page.events
            ],
            next_cursor=page.next_cursor,
        )
