"""HTTP surface for the facility staff roster and staff check-in/out (V1-04C), registered by
``main``.

Conventions follow ``classroom_api``: bearer principal, permission dependency, tenant from the
identity mapping only, unknown / other-tenant / unreadable-facility identifiers all answer 404,
bounded error bodies carrying a category and never an echo of input.

Every write names a staff profile explicitly and comes from an authenticated operator. There is
no route through which a camera, a recognition result or an edge node records presence.
Wording: a designation *counts toward the configured classroom ratio*; nothing here says a
person is licensed, certified or legally qualified.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from veotrex_api.access import PrincipalContext, require_permission
from veotrex_api.authorization import Permission
from veotrex_api.classroom_service import ClassroomError
from veotrex_api.staff_presence import STAFF_LEASE_DEFAULT_SECONDS
from veotrex_api.staff_roster_service import (
    ROSTER_CONFLICT_CATEGORIES,
    ROSTER_VALIDATION_CATEGORIES,
    ClassroomStaffPresence,
    EligibilityChange,
    EligibilityInput,
    EligibilitySummary,
    FacilityRoster,
    StaffRosterService,
)

require_read_operational = require_permission(Permission.READ_OPERATIONAL)
require_administer_facility = require_permission(Permission.ADMINISTER_FACILITY)


# ------------------------------------------------------------------------------ requests
class EligibilityCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    staff_profile_id: UUID
    counts_toward_ratio: bool = Field(strict=True)
    note: str | None = Field(default=None, max_length=500)
    effective_from_date: date | None = None
    effective_through_date: date | None = None


class EligibilityUpdateRequest(BaseModel):
    """Absent leaves a field unchanged; present-and-null clears ``note`` or the end date."""

    model_config = ConfigDict(extra="forbid")
    counts_toward_ratio: bool | None = Field(default=None, strict=True)
    note: str | None = Field(default=None, max_length=500)
    effective_through_date: date | None = None


class StaffPresenceRequest(BaseModel):
    """Who, and for how long. No timestamp (the server's clock is used), no image, no track."""

    model_config = ConfigDict(extra="forbid")
    staff_profile_id: UUID
    lease_seconds: int = Field(default=STAFF_LEASE_DEFAULT_SECONDS, strict=True)


class StaffCheckOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    staff_profile_id: UUID


# ----------------------------------------------------------------------------- responses
class EligibilityResponse(BaseModel):
    eligibility_id: str
    facility_id: str
    staff_profile_id: str
    staff_display_name: str
    staff_status: str
    status: str
    counts_toward_ratio: bool
    note: str | None
    effective_from: str
    effective_until: str | None
    effective_from_date: str
    effective_through_date: str | None
    in_effect: bool
    revision: int
    created_at: str
    updated_at: str
    deactivated_at: str | None
    eligibility_basis: str = "OPERATOR_DESIGNATED"


class FacilityRosterResponse(BaseModel):
    facility_id: str
    facility_name: str
    facility_timezone: str
    can_administer: bool
    assignments: list[EligibilityResponse]


class StaffCountResponse(BaseModel):
    source: str
    count: int
    present: int
    present_ratio_ineligible: int
    present_inactive: int
    present_ambiguous: int
    stale: int
    freshness: str
    valid_until: str | None
    evaluated_at: str


class StaffPresenceEntryResponse(BaseModel):
    staff_profile_id: str
    display_name: str
    staff_status: str
    on_facility_roster: bool
    counts_toward_ratio: bool
    counted: bool
    state: str
    location: str
    other_classroom_id: str | None
    other_classroom_name: str | None
    checked_in_at: str | None
    last_event_at: str | None
    valid_until: str | None


class StaffPresenceEventResponse(BaseModel):
    event_id: str
    staff_profile_id: str
    display_name: str
    event_type: str
    occurred_at: str
    valid_until: str | None
    recorded_by_caller: bool


class StaffPresenceResponse(BaseModel):
    classroom_id: str
    facility_id: str
    classroom_active: bool
    presence_source_mode: str
    can_administer: bool
    evaluated_at: str
    lease_min_seconds: int
    lease_max_seconds: int
    lease_default_seconds: int
    summary: StaffCountResponse
    staff: list[StaffPresenceEntryResponse]
    recent_events: list[StaffPresenceEventResponse]


def _iso(value: date | None) -> str | None:
    # ``datetime`` is a ``date``: one helper serves timestamps and facility-local days.
    return None if value is None else value.isoformat()


def _eligibility(value: EligibilitySummary) -> EligibilityResponse:
    return EligibilityResponse(
        eligibility_id=str(value.eligibility_id),
        facility_id=str(value.facility_id),
        staff_profile_id=str(value.staff_profile_id),
        staff_display_name=value.staff_display_name,
        staff_status=value.staff_status,
        status=value.status,
        counts_toward_ratio=value.counts_toward_ratio,
        note=value.note,
        effective_from=value.effective_from.isoformat(),
        effective_until=_iso(value.effective_until),
        effective_from_date=value.effective_from_date.isoformat(),
        effective_through_date=_iso(value.effective_through_date),
        in_effect=value.in_effect,
        revision=value.revision,
        created_at=value.created_at.isoformat(),
        updated_at=value.updated_at.isoformat(),
        deactivated_at=_iso(value.deactivated_at),
    )


def _roster(value: FacilityRoster) -> FacilityRosterResponse:
    return FacilityRosterResponse(
        facility_id=str(value.facility_id),
        facility_name=value.facility_name,
        facility_timezone=value.facility_timezone,
        can_administer=value.can_administer,
        assignments=[_eligibility(item) for item in value.assignments],
    )


def _presence(value: ClassroomStaffPresence) -> StaffPresenceResponse:
    summary = value.summary.as_dict()
    return StaffPresenceResponse(
        classroom_id=str(value.classroom_id),
        facility_id=str(value.facility_id),
        classroom_active=value.classroom_active,
        presence_source_mode=value.presence_source_mode,
        can_administer=value.can_administer,
        evaluated_at=value.evaluated_at.isoformat(),
        lease_min_seconds=value.lease_min_seconds,
        lease_max_seconds=value.lease_max_seconds,
        lease_default_seconds=value.lease_default_seconds,
        summary=StaffCountResponse(**summary),
        staff=[
            StaffPresenceEntryResponse(
                staff_profile_id=str(entry.staff_profile_id),
                display_name=entry.display_name,
                staff_status=entry.staff_status,
                on_facility_roster=entry.on_facility_roster,
                counts_toward_ratio=entry.counts_toward_ratio,
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
            for entry in value.staff
        ],
        recent_events=[
            StaffPresenceEventResponse(
                event_id=str(event.event_id),
                staff_profile_id=str(event.staff_profile_id),
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
    if exc.category in ROSTER_VALIDATION_CATEGORIES:
        return HTTPException(
            status_code=422,
            detail={"message": "staff roster request rejected", "category": exc.category},
        )
    if exc.category in ROSTER_CONFLICT_CATEGORIES:
        return HTTPException(
            status_code=409,
            detail={"message": "staff roster request conflicts", "category": exc.category},
        )
    return HTTPException(status_code=409, detail="staff roster request could not be completed")


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id", str(uuid4()))


def register_staff_roster_routes(app: FastAPI, service: StaffRosterService) -> None:
    base = "/v1/facilities/{facility_id}/staff-ratio-eligibility"

    @app.get(base, response_model=FacilityRosterResponse)
    async def list_eligibility(
        facility_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
        staff_profile_id: Annotated[UUID | None, Query()] = None,
    ) -> FacilityRosterResponse:
        try:
            return _roster(
                await service.list_eligibility(
                    context.principal, facility_id, staff_profile_id=staff_profile_id
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(base, response_model=EligibilityResponse, status_code=201)
    async def create_eligibility(
        facility_id: UUID,
        payload: EligibilityCreateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> EligibilityResponse:
        try:
            return _eligibility(
                await service.create_eligibility(
                    context.principal,
                    facility_id,
                    EligibilityInput(
                        staff_profile_id=payload.staff_profile_id,
                        counts_toward_ratio=payload.counts_toward_ratio,
                        note=payload.note,
                        effective_from_date=payload.effective_from_date,
                        effective_through_date=payload.effective_through_date,
                    ),
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.patch(base + "/{eligibility_id}", response_model=EligibilityResponse)
    async def update_eligibility(
        facility_id: UUID,
        eligibility_id: UUID,
        payload: EligibilityUpdateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> EligibilityResponse:
        given = payload.model_fields_set
        try:
            return _eligibility(
                await service.update_eligibility(
                    context.principal,
                    facility_id,
                    eligibility_id,
                    EligibilityChange(
                        counts_toward_ratio=payload.counts_toward_ratio,
                        note=payload.note,
                        set_note="note" in given,
                        effective_through_date=payload.effective_through_date,
                        set_effective_through="effective_through_date" in given,
                    ),
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(base + "/{eligibility_id}/deactivate", response_model=EligibilityResponse)
    async def deactivate_eligibility(
        facility_id: UUID,
        eligibility_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> EligibilityResponse:
        try:
            return _eligibility(
                await service.deactivate_eligibility(
                    context.principal, facility_id, eligibility_id, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    presence = "/v1/classrooms/{classroom_id}/staff-presence"

    @app.get(presence, response_model=StaffPresenceResponse)
    async def get_staff_presence(
        classroom_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> StaffPresenceResponse:
        try:
            return _presence(await service.get_staff_presence(context.principal, classroom_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(presence + "/check-in", response_model=StaffPresenceResponse, status_code=201)
    async def check_in(
        classroom_id: UUID,
        payload: StaffPresenceRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> StaffPresenceResponse:
        try:
            return _presence(
                await service.check_in(
                    context.principal,
                    classroom_id,
                    payload.staff_profile_id,
                    _request_id(request),
                    lease_seconds=payload.lease_seconds,
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(presence + "/refresh", response_model=StaffPresenceResponse)
    async def refresh(
        classroom_id: UUID,
        payload: StaffPresenceRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> StaffPresenceResponse:
        try:
            return _presence(
                await service.refresh(
                    context.principal,
                    classroom_id,
                    payload.staff_profile_id,
                    _request_id(request),
                    lease_seconds=payload.lease_seconds,
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(presence + "/check-out", response_model=StaffPresenceResponse)
    async def check_out(
        classroom_id: UUID,
        payload: StaffCheckOutRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> StaffPresenceResponse:
        try:
            return _presence(
                await service.check_out(
                    context.principal, classroom_id, payload.staff_profile_id, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None
