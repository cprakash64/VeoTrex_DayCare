"""HTTP surface for the facility child roster and child attendance (V1-04D), registered by
``main``.

Human routes only: every route requires an Auth0-authenticated operator principal, so an edge
machine credential opens none of them, and nothing here lives under ``/v1/edge``. Conventions
follow ``classroom_api``: tenant from the identity mapping only; unknown, other-tenant,
other-facility and unreadable identifiers all answer a uniform 404; bounded error bodies carry a
category and never echo input - in particular never a child's name.

Attendance requests name a child profile by UUID and optionally a bounded lease. There is no
field for a timestamp, a camera, a person track, an image or a recognition result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from veotrex_api.access import PrincipalContext, require_permission
from veotrex_api.authorization import Permission
from veotrex_api.child_attendance import ATTENDANCE_LEASE_DEFAULT_SECONDS
from veotrex_api.child_roster_service import (
    CHILD_CONFLICT_CATEGORIES,
    CHILD_VALIDATION_CATEGORIES,
    ChildRosterService,
    ChildSummary,
    ClassroomAttendance,
    FacilityChildren,
)
from veotrex_api.classroom_service import ClassroomError

require_read_operational = require_permission(Permission.READ_OPERATIONAL)
require_administer_facility = require_permission(Permission.ADMINISTER_FACILITY)


# ------------------------------------------------------------------------------ requests
class ChildCreateRequest(BaseModel):
    """A display name and an optional identifier. Nothing else about a child is accepted."""

    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=400)
    external_reference: str | None = Field(default=None, max_length=64)


class ChildUpdateRequest(BaseModel):
    """Absent leaves a field unchanged; ``external_reference: null`` clears it."""

    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=400)
    external_reference: str | None = Field(default=None, max_length=64)


class AttendanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    child_profile_id: UUID
    lease_seconds: int = Field(default=ATTENDANCE_LEASE_DEFAULT_SECONDS, strict=True)


class AttendanceCheckOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    child_profile_id: UUID


# ----------------------------------------------------------------------------- responses
class ChildResponse(BaseModel):
    child_id: str
    facility_id: str
    display_name: str
    status: str
    external_reference: str | None
    can_administer: bool
    created_at: str
    updated_at: str


class FacilityChildrenResponse(BaseModel):
    facility_id: str
    facility_name: str
    can_administer: bool
    children: list[ChildResponse]


class ChildCountResponse(BaseModel):
    source: str
    count: int
    present: int
    present_inactive: int
    stale: int
    freshness: str
    valid_until: str | None
    evaluated_at: str


class AttendanceEntryResponse(BaseModel):
    child_profile_id: str
    display_name: str
    status: str
    counted: bool
    state: str
    location: str
    other_classroom_id: str | None
    other_classroom_name: str | None
    checked_in_at: str | None
    last_event_at: str | None
    valid_until: str | None


class AttendanceEventResponse(BaseModel):
    event_id: str
    child_profile_id: str
    display_name: str
    event_type: str
    occurred_at: str
    valid_until: str | None
    recorded_by_caller: bool


class AttendanceResponse(BaseModel):
    classroom_id: str
    facility_id: str
    classroom_active: bool
    presence_source_mode: str
    can_administer: bool
    evaluated_at: str
    lease_min_seconds: int
    lease_max_seconds: int
    lease_default_seconds: int
    summary: ChildCountResponse
    children: list[AttendanceEntryResponse]
    recent_events: list[AttendanceEventResponse]


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _child(value: ChildSummary) -> ChildResponse:
    return ChildResponse(
        child_id=str(value.child_id),
        facility_id=str(value.facility_id),
        display_name=value.display_name,
        status=value.status,
        external_reference=value.external_reference,
        can_administer=value.can_administer,
        created_at=value.created_at.isoformat(),
        updated_at=value.updated_at.isoformat(),
    )


def _children(value: FacilityChildren) -> FacilityChildrenResponse:
    return FacilityChildrenResponse(
        facility_id=str(value.facility_id),
        facility_name=value.facility_name,
        can_administer=value.can_administer,
        children=[_child(item) for item in value.children],
    )


def _attendance(value: ClassroomAttendance) -> AttendanceResponse:
    return AttendanceResponse(
        classroom_id=str(value.classroom_id),
        facility_id=str(value.facility_id),
        classroom_active=value.classroom_active,
        presence_source_mode=value.presence_source_mode,
        can_administer=value.can_administer,
        evaluated_at=value.evaluated_at.isoformat(),
        lease_min_seconds=value.lease_min_seconds,
        lease_max_seconds=value.lease_max_seconds,
        lease_default_seconds=value.lease_default_seconds,
        summary=ChildCountResponse(**value.summary.as_dict()),
        children=[
            AttendanceEntryResponse(
                child_profile_id=str(entry.child_profile_id),
                display_name=entry.display_name,
                status=entry.status,
                counted=entry.counted,
                state=entry.state,
                location=entry.location,
                other_classroom_id=None
                if entry.other_classroom_id is None
                else str(entry.other_classroom_id),
                other_classroom_name=entry.other_classroom_name,
                checked_in_at=_iso(entry.checked_in_at),
                last_event_at=_iso(entry.last_event_at),
                valid_until=_iso(entry.valid_until),
            )
            for entry in value.children
        ],
        recent_events=[
            AttendanceEventResponse(
                event_id=str(event.event_id),
                child_profile_id=str(event.child_profile_id),
                display_name=event.display_name,
                event_type=event.event_type,
                occurred_at=event.occurred_at.isoformat(),
                valid_until=_iso(event.valid_until),
                recorded_by_caller=event.recorded_by_caller,
            )
            for event in value.recent_events
        ],
    )


def _http(exc: ClassroomError) -> HTTPException:
    if exc.category == "not_found":
        return HTTPException(status_code=404, detail="not found")
    if exc.category == "access_denied":
        return HTTPException(status_code=403, detail="access denied")
    if exc.category in CHILD_VALIDATION_CATEGORIES:
        return HTTPException(
            status_code=422,
            detail={"message": "child roster request rejected", "category": exc.category},
        )
    if exc.category in CHILD_CONFLICT_CATEGORIES:
        return HTTPException(
            status_code=409,
            detail={"message": "child roster request conflicts", "category": exc.category},
        )
    return HTTPException(status_code=409, detail="child roster request could not be completed")


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id", str(uuid4()))


def register_child_roster_routes(app: FastAPI, service: ChildRosterService) -> None:
    @app.get("/v1/facilities/{facility_id}/children", response_model=FacilityChildrenResponse)
    async def list_children(
        facility_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> FacilityChildrenResponse:
        try:
            return _children(await service.list_children(context.principal, facility_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(
        "/v1/facilities/{facility_id}/children", response_model=ChildResponse, status_code=201
    )
    async def create_child(
        facility_id: UUID,
        payload: ChildCreateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ChildResponse:
        try:
            return _child(
                await service.create_child(
                    context.principal,
                    facility_id,
                    payload.display_name,
                    payload.external_reference,
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.get("/v1/children/{child_id}", response_model=ChildResponse)
    async def get_child(
        child_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> ChildResponse:
        try:
            return _child(await service.get_child(context.principal, child_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.patch("/v1/children/{child_id}", response_model=ChildResponse)
    async def update_child(
        child_id: UUID,
        payload: ChildUpdateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ChildResponse:
        try:
            return _child(
                await service.update_child(
                    context.principal,
                    child_id,
                    _request_id(request),
                    display_name=payload.display_name,
                    external_reference=payload.external_reference,
                    set_external_reference="external_reference" in payload.model_fields_set,
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    async def set_status(
        context: PrincipalContext, child_id: UUID, status: str, request: Request
    ) -> ChildResponse:
        try:
            return _child(
                await service.set_child_status(
                    context.principal, child_id, status, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post("/v1/children/{child_id}/activate", response_model=ChildResponse)
    async def activate_child(
        child_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ChildResponse:
        return await set_status(context, child_id, "ACTIVE", request)

    @app.post("/v1/children/{child_id}/deactivate", response_model=ChildResponse)
    async def deactivate_child(
        child_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ChildResponse:
        return await set_status(context, child_id, "INACTIVE", request)

    @app.post("/v1/children/{child_id}/archive", response_model=ChildResponse)
    async def archive_child(
        child_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ChildResponse:
        return await set_status(context, child_id, "ARCHIVED", request)

    base = "/v1/classrooms/{classroom_id}/attendance"

    @app.get(base, response_model=AttendanceResponse)
    async def get_attendance(
        classroom_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> AttendanceResponse:
        try:
            return _attendance(await service.get_attendance(context.principal, classroom_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(base + "/check-in", response_model=AttendanceResponse, status_code=201)
    async def check_in(
        classroom_id: UUID,
        payload: AttendanceRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> AttendanceResponse:
        try:
            return _attendance(
                await service.check_in(
                    context.principal,
                    classroom_id,
                    payload.child_profile_id,
                    _request_id(request),
                    lease_seconds=payload.lease_seconds,
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(base + "/refresh", response_model=AttendanceResponse)
    async def refresh(
        classroom_id: UUID,
        payload: AttendanceRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> AttendanceResponse:
        try:
            return _attendance(
                await service.refresh(
                    context.principal,
                    classroom_id,
                    payload.child_profile_id,
                    _request_id(request),
                    lease_seconds=payload.lease_seconds,
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(base + "/check-out", response_model=AttendanceResponse)
    async def check_out(
        classroom_id: UUID,
        payload: AttendanceCheckOutRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> AttendanceResponse:
        try:
            return _attendance(
                await service.check_out(
                    context.principal, classroom_id, payload.child_profile_id, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None
