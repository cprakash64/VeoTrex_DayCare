"""HTTP surface for guardian contacts, child associations and authorized release (V1-04E),
registered by ``main``.

Human routes only: every route requires an Auth0-authenticated operator principal, so an edge
machine credential opens none of them, and nothing here lives under ``/v1/edge`` or
``/v1/integrations``. Conventions follow ``child_roster_api``: tenant from the identity mapping
only; unknown, other-tenant and unreadable identifiers all answer a uniform 404; bounded error
bodies carry a category and never echo input - in particular never a name or a label.

A release request names a child, a guardian contact and the operator's verification method.
There is no field for a timestamp, a camera, a person track, an image, a recognition result, an
identity-document number or a free-text note, and unknown fields are refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from veotrex_api.access import PrincipalContext, require_permission
from veotrex_api.authorization import Permission
from veotrex_api.child_roster_api import AttendanceResponse, attendance_response
from veotrex_api.classroom_service import ClassroomError
from veotrex_api.guardian_release import VerificationMethod
from veotrex_api.guardian_service import (
    GUARDIAN_CONFLICT_CATEGORIES,
    GUARDIAN_VALIDATION_CATEGORIES,
    ChildGuardians,
    ChildReleaseHistory,
    ClassroomReleaseOptions,
    FacilityGuardians,
    GuardianService,
    GuardianSummary,
    LinkChange,
    LinkInput,
    ReleaseSummary,
)

require_read_operational = require_permission(Permission.READ_OPERATIONAL)
require_administer_facility = require_permission(Permission.ADMINISTER_FACILITY)


# ------------------------------------------------------------------------------ requests
class GuardianCreateRequest(BaseModel):
    """A display name and an optional identifier. Nothing else about an adult is accepted."""

    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=400)
    external_reference: str | None = Field(default=None, max_length=64)


class GuardianUpdateRequest(BaseModel):
    """Absent leaves a field unchanged; ``external_reference: null`` clears it."""

    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=400)
    external_reference: str | None = Field(default=None, max_length=64)


class LinkCreateRequest(BaseModel):
    """``pickup_authorized`` has no default: the operator decides it explicitly, and the
    relationship label never implies it. Times must carry a UTC offset."""

    model_config = ConfigDict(extra="forbid")
    guardian_contact_id: UUID
    relationship_label: str = Field(min_length=1, max_length=200)
    pickup_authorized: bool = Field(strict=True)
    effective_from: AwareDatetime | None = None
    effective_until: AwareDatetime | None = None
    note: str | None = Field(default=None, max_length=400)


class LinkUpdateRequest(BaseModel):
    """Absent leaves a field unchanged; ``effective_until: null`` makes the authorization
    open-ended and ``note: null`` clears the note."""

    model_config = ConfigDict(extra="forbid")
    relationship_label: str | None = Field(default=None, min_length=1, max_length=200)
    pickup_authorized: bool | None = Field(default=None, strict=True)
    effective_from: AwareDatetime | None = None
    effective_until: AwareDatetime | None = None
    note: str | None = Field(default=None, max_length=400)


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    child_profile_id: UUID
    guardian_contact_id: UUID
    verification_method: VerificationMethod


# ----------------------------------------------------------------------------- responses
class GuardianResponse(BaseModel):
    guardian_contact_id: str
    facility_id: str
    display_name: str
    status: str
    external_reference: str | None
    active_link_count: int
    can_administer: bool
    created_at: str
    updated_at: str


class FacilityGuardiansResponse(BaseModel):
    facility_id: str
    facility_name: str
    facility_timezone: str
    can_administer: bool
    guardians: list[GuardianResponse]


class LinkResponse(BaseModel):
    link_id: str
    child_profile_id: str
    guardian_contact_id: str
    guardian_display_name: str
    guardian_status: str
    relationship_label: str
    pickup_authorized: bool
    effective_from: str
    effective_until: str | None
    status: str
    note: str | None
    revision: int
    pickup_status: str
    created_at: str
    updated_at: str
    deactivated_at: str | None


class ChildGuardiansResponse(BaseModel):
    child_profile_id: str
    child_display_name: str
    child_status: str
    facility_id: str
    facility_timezone: str
    can_administer: bool
    evaluated_at: str
    links: list[LinkResponse]


class ReleaseRecordResponse(BaseModel):
    release_id: str
    classroom_id: str
    classroom_name: str
    child_profile_id: str
    guardian_contact_id: str
    guardian_display_name: str
    authorization_link_id: str
    authorization_link_revision: int
    verification_method: str
    released_at: str
    attendance_event_id: str
    recorded_by_caller: bool


class ReleaseHistoryResponse(BaseModel):
    child_profile_id: str
    releases: list[ReleaseRecordResponse]


class ReleaseCandidateResponse(BaseModel):
    guardian_contact_id: str
    display_name: str
    relationship_label: str
    link_id: str
    effective_until: str | None


class UnavailableContactResponse(BaseModel):
    guardian_contact_id: str
    display_name: str
    relationship_label: str
    reason: str


class ChildReleaseOptionsResponse(BaseModel):
    child_profile_id: str
    display_name: str
    candidates: list[ReleaseCandidateResponse]
    unavailable: list[UnavailableContactResponse]


class ReleaseOptionsResponse(BaseModel):
    classroom_id: str
    can_release: bool
    evaluated_at: str
    verification_methods: list[str]
    children: list[ChildReleaseOptionsResponse]


class ReleaseResponse(BaseModel):
    release: ReleaseRecordResponse
    attendance: AttendanceResponse


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _guardian(value: GuardianSummary) -> GuardianResponse:
    return GuardianResponse(
        guardian_contact_id=str(value.guardian_contact_id),
        facility_id=str(value.facility_id),
        display_name=value.display_name,
        status=value.status,
        external_reference=value.external_reference,
        active_link_count=value.active_link_count,
        can_administer=value.can_administer,
        created_at=value.created_at.isoformat(),
        updated_at=value.updated_at.isoformat(),
    )


def _guardians(value: FacilityGuardians) -> FacilityGuardiansResponse:
    return FacilityGuardiansResponse(
        facility_id=str(value.facility_id),
        facility_name=value.facility_name,
        facility_timezone=value.facility_timezone,
        can_administer=value.can_administer,
        guardians=[_guardian(item) for item in value.guardians],
    )


def _child_guardians(value: ChildGuardians) -> ChildGuardiansResponse:
    return ChildGuardiansResponse(
        child_profile_id=str(value.child_profile_id),
        child_display_name=value.child_display_name,
        child_status=value.child_status,
        facility_id=str(value.facility_id),
        facility_timezone=value.facility_timezone,
        can_administer=value.can_administer,
        evaluated_at=value.evaluated_at.isoformat(),
        links=[
            LinkResponse(
                link_id=str(link.link_id),
                child_profile_id=str(link.child_profile_id),
                guardian_contact_id=str(link.guardian_contact_id),
                guardian_display_name=link.guardian_display_name,
                guardian_status=link.guardian_status,
                relationship_label=link.relationship_label,
                pickup_authorized=link.pickup_authorized,
                effective_from=link.effective_from.isoformat(),
                effective_until=_iso(link.effective_until),
                status=link.status,
                note=link.note,
                revision=link.revision,
                pickup_status=link.pickup_status,
                created_at=link.created_at.isoformat(),
                updated_at=link.updated_at.isoformat(),
                deactivated_at=_iso(link.deactivated_at),
            )
            for link in value.links
        ],
    )


def _release(value: ReleaseSummary) -> ReleaseRecordResponse:
    return ReleaseRecordResponse(
        release_id=str(value.release_id),
        classroom_id=str(value.classroom_id),
        classroom_name=value.classroom_name,
        child_profile_id=str(value.child_profile_id),
        guardian_contact_id=str(value.guardian_contact_id),
        guardian_display_name=value.guardian_display_name,
        authorization_link_id=str(value.authorization_link_id),
        authorization_link_revision=value.authorization_link_revision,
        verification_method=value.verification_method,
        released_at=value.released_at.isoformat(),
        attendance_event_id=str(value.attendance_event_id),
        recorded_by_caller=value.recorded_by_caller,
    )


def _history(value: ChildReleaseHistory) -> ReleaseHistoryResponse:
    return ReleaseHistoryResponse(
        child_profile_id=str(value.child_profile_id),
        releases=[_release(item) for item in value.releases],
    )


def _options(value: ClassroomReleaseOptions) -> ReleaseOptionsResponse:
    return ReleaseOptionsResponse(
        classroom_id=str(value.classroom_id),
        can_release=value.can_release,
        evaluated_at=value.evaluated_at.isoformat(),
        verification_methods=list(value.verification_methods),
        children=[
            ChildReleaseOptionsResponse(
                child_profile_id=str(child.child_profile_id),
                display_name=child.display_name,
                candidates=[
                    ReleaseCandidateResponse(
                        guardian_contact_id=str(item.guardian_contact_id),
                        display_name=item.display_name,
                        relationship_label=item.relationship_label,
                        link_id=str(item.link_id),
                        effective_until=_iso(item.effective_until),
                    )
                    for item in child.candidates
                ],
                unavailable=[
                    UnavailableContactResponse(
                        guardian_contact_id=str(item.guardian_contact_id),
                        display_name=item.display_name,
                        relationship_label=item.relationship_label,
                        reason=item.reason,
                    )
                    for item in child.unavailable
                ],
            )
            for child in value.children
        ],
    )


def _http(exc: ClassroomError) -> HTTPException:
    if exc.category == "not_found":
        return HTTPException(status_code=404, detail="not found")
    if exc.category == "access_denied":
        return HTTPException(status_code=403, detail="access denied")
    if exc.category in GUARDIAN_VALIDATION_CATEGORIES:
        return HTTPException(
            status_code=422,
            detail={"message": "guardian request rejected", "category": exc.category},
        )
    if exc.category in GUARDIAN_CONFLICT_CATEGORIES:
        return HTTPException(
            status_code=409,
            detail={"message": "guardian request conflicts", "category": exc.category},
        )
    return HTTPException(status_code=409, detail="guardian request could not be completed")


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id", str(uuid4()))


def register_guardian_routes(app: FastAPI, service: GuardianService) -> None:
    # --------------------------------------------------------------------- contacts
    @app.get("/v1/facilities/{facility_id}/guardians", response_model=FacilityGuardiansResponse)
    async def list_guardians(
        facility_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> FacilityGuardiansResponse:
        try:
            return _guardians(await service.list_guardians(context.principal, facility_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(
        "/v1/facilities/{facility_id}/guardians", response_model=GuardianResponse, status_code=201
    )
    async def create_guardian(
        facility_id: UUID,
        payload: GuardianCreateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> GuardianResponse:
        try:
            return _guardian(
                await service.create_guardian(
                    context.principal,
                    facility_id,
                    payload.display_name,
                    payload.external_reference,
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.get("/v1/guardians/{guardian_id}", response_model=GuardianResponse)
    async def get_guardian(
        guardian_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> GuardianResponse:
        try:
            return _guardian(await service.get_guardian(context.principal, guardian_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.patch("/v1/guardians/{guardian_id}", response_model=GuardianResponse)
    async def update_guardian(
        guardian_id: UUID,
        payload: GuardianUpdateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> GuardianResponse:
        try:
            return _guardian(
                await service.update_guardian(
                    context.principal,
                    guardian_id,
                    _request_id(request),
                    display_name=payload.display_name,
                    external_reference=payload.external_reference,
                    set_external_reference="external_reference" in payload.model_fields_set,
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    async def set_status(
        context: PrincipalContext, guardian_id: UUID, status: str, request: Request
    ) -> GuardianResponse:
        try:
            return _guardian(
                await service.set_guardian_status(
                    context.principal, guardian_id, status, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post("/v1/guardians/{guardian_id}/activate", response_model=GuardianResponse)
    async def activate_guardian(
        guardian_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> GuardianResponse:
        return await set_status(context, guardian_id, "ACTIVE", request)

    @app.post("/v1/guardians/{guardian_id}/deactivate", response_model=GuardianResponse)
    async def deactivate_guardian(
        guardian_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> GuardianResponse:
        return await set_status(context, guardian_id, "INACTIVE", request)

    @app.post("/v1/guardians/{guardian_id}/archive", response_model=GuardianResponse)
    async def archive_guardian(
        guardian_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> GuardianResponse:
        return await set_status(context, guardian_id, "ARCHIVED", request)

    # ------------------------------------------------------------------------ links
    links = "/v1/children/{child_id}/guardians"

    @app.get(links, response_model=ChildGuardiansResponse)
    async def list_child_guardians(
        child_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> ChildGuardiansResponse:
        try:
            return _child_guardians(await service.list_child_guardians(context.principal, child_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(links, response_model=ChildGuardiansResponse, status_code=201)
    async def create_link(
        child_id: UUID,
        payload: LinkCreateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ChildGuardiansResponse:
        try:
            return _child_guardians(
                await service.create_link(
                    context.principal,
                    child_id,
                    LinkInput(
                        guardian_contact_id=payload.guardian_contact_id,
                        relationship_label=payload.relationship_label,
                        pickup_authorized=payload.pickup_authorized,
                        effective_from=payload.effective_from,
                        effective_until=payload.effective_until,
                        note=payload.note,
                    ),
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.patch(links + "/{link_id}", response_model=ChildGuardiansResponse)
    async def update_link(
        child_id: UUID,
        link_id: UUID,
        payload: LinkUpdateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ChildGuardiansResponse:
        fields = payload.model_fields_set
        try:
            return _child_guardians(
                await service.update_link(
                    context.principal,
                    child_id,
                    link_id,
                    LinkChange(
                        relationship_label=payload.relationship_label,
                        pickup_authorized=payload.pickup_authorized,
                        effective_from=payload.effective_from,
                        effective_until=payload.effective_until,
                        set_effective_until="effective_until" in fields,
                        note=payload.note,
                        set_note="note" in fields,
                    ),
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(links + "/{link_id}/deactivate", response_model=ChildGuardiansResponse)
    async def deactivate_link(
        child_id: UUID,
        link_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ChildGuardiansResponse:
        try:
            return _child_guardians(
                await service.deactivate_link(
                    context.principal, child_id, link_id, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    # ---------------------------------------------------------------------- release
    @app.get("/v1/children/{child_id}/release-history", response_model=ReleaseHistoryResponse)
    async def release_history(
        child_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> ReleaseHistoryResponse:
        try:
            return _history(await service.release_history(context.principal, child_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    base = "/v1/classrooms/{classroom_id}/attendance"

    @app.get(base + "/release-options", response_model=ReleaseOptionsResponse)
    async def release_options(
        classroom_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> ReleaseOptionsResponse:
        try:
            return _options(await service.release_options(context.principal, classroom_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(base + "/release", response_model=ReleaseResponse, status_code=201)
    async def release_child(
        classroom_id: UUID,
        payload: ReleaseRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ReleaseResponse:
        try:
            result = await service.release_child(
                context.principal,
                classroom_id,
                payload.child_profile_id,
                payload.guardian_contact_id,
                str(payload.verification_method),
                _request_id(request),
            )
        except ClassroomError as exc:
            raise _http(exc) from None
        return ReleaseResponse(
            release=_release(result.release), attendance=attendance_response(result.attendance)
        )
