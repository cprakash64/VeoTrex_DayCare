"""HTTP surface for classrooms and configured ratio policies (V1-04A), registered by ``main``.

Conventions follow ``staff_api``: bearer principal, permission dependency, tenant from the
identity mapping only, unknown / other-tenant / unreadable-facility identifiers all answer 404,
bounded error bodies carrying a category and never an echo of input.

Wording is deliberate. A policy here is the operator's *configured classroom policy*; nothing
in a response says "compliant", "legal" or names a jurisdiction, because nothing here verified
one.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from veotrex_api.access import PrincipalContext, require_permission
from veotrex_api.authorization import Permission
from veotrex_api.classroom_service import (
    CONFLICT_CATEGORIES,
    VALIDATION_CATEGORIES,
    ClassroomError,
    ClassroomService,
    ClassroomSummary,
    PolicyInput,
    PolicySummary,
)

require_read_operational = require_permission(Permission.READ_OPERATIONAL)
require_administer_facility = require_permission(Permission.ADMINISTER_FACILITY)


class ClassroomCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facility_id: UUID
    name: str = Field(min_length=1, max_length=200)
    age_band_label: str | None = Field(default=None, max_length=64)


class ClassroomUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    # Present-and-null clears the label; absent leaves it unchanged.
    age_band_label: str | None = Field(default=None, max_length=64)


class PolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=120)
    age_band_label: str | None = Field(default=None, max_length=64)
    max_children_per_staff: int = Field(strict=True)
    minimum_staff: int = Field(strict=True)
    maximum_group_size: int | None = Field(default=None, strict=True)
    # Local calendar days in the facility's timezone. ``effective_through_date`` is the last day
    # the policy applies (inclusive); omitted means open-ended.
    effective_from_date: date
    effective_through_date: date | None = None
    source_reference: str | None = Field(default=None, max_length=500)

    def to_input(self) -> PolicyInput:
        return PolicyInput(
            label=self.label,
            age_band_label=self.age_band_label,
            max_children_per_staff=self.max_children_per_staff,
            minimum_staff=self.minimum_staff,
            maximum_group_size=self.maximum_group_size,
            effective_from_date=self.effective_from_date,
            effective_through_date=self.effective_through_date,
            source_reference=self.source_reference,
        )


class FacilityResponse(BaseModel):
    facility_id: str
    name: str
    timezone: str
    status: str
    can_administer: bool


class CameraResponse(BaseModel):
    camera_id: str
    name: str
    status: str
    zone_name: str


class PolicyResponse(BaseModel):
    policy_id: str
    label: str
    age_band_label: str | None
    max_children_per_staff: int
    minimum_staff: int
    maximum_group_size: int | None
    effective_from: str
    effective_until: str | None
    effective_from_date: str
    effective_through_date: str | None
    status: str
    revision: int
    source_reference: str | None
    in_effect: bool
    created_at: str
    updated_at: str


class ClassroomResponse(BaseModel):
    classroom_id: str
    facility_id: str
    facility_name: str
    facility_timezone: str
    name: str
    status: str
    age_band_label: str | None
    cameras: list[CameraResponse]
    policies: list[PolicyResponse]
    current_policy_id: str | None
    can_administer: bool
    policy_basis: str = "CONFIGURED_CLASSROOM_POLICY"
    created_at: str
    updated_at: str


def _policy(value: PolicySummary) -> PolicyResponse:
    return PolicyResponse(
        policy_id=str(value.policy_id),
        label=value.label,
        age_band_label=value.age_band_label,
        max_children_per_staff=value.max_children_per_staff,
        minimum_staff=value.minimum_staff,
        maximum_group_size=value.maximum_group_size,
        effective_from=value.effective_from.isoformat(),
        effective_until=None
        if value.effective_until is None
        else value.effective_until.isoformat(),
        effective_from_date=value.effective_from_date.isoformat(),
        effective_through_date=None
        if value.effective_through_date is None
        else value.effective_through_date.isoformat(),
        status=value.status,
        revision=value.revision,
        source_reference=value.source_reference,
        in_effect=value.in_effect,
        created_at=value.created_at.isoformat(),
        updated_at=value.updated_at.isoformat(),
    )


def _classroom(value: ClassroomSummary) -> ClassroomResponse:
    return ClassroomResponse(
        classroom_id=str(value.classroom_id),
        facility_id=str(value.facility_id),
        facility_name=value.facility_name,
        facility_timezone=value.facility_timezone,
        name=value.name,
        status=value.status,
        age_band_label=value.age_band_label,
        cameras=[
            CameraResponse(
                camera_id=str(camera.camera_id),
                name=camera.name,
                status=camera.status,
                zone_name=camera.zone_name,
            )
            for camera in value.cameras
        ],
        policies=[_policy(policy) for policy in value.policies],
        current_policy_id=None if value.current_policy_id is None else str(value.current_policy_id),
        can_administer=value.can_administer,
        created_at=value.created_at.isoformat(),
        updated_at=value.updated_at.isoformat(),
    )


def _http(exc: ClassroomError) -> HTTPException:
    if exc.category == "not_found":
        return HTTPException(status_code=404, detail="classroom not found")
    if exc.category == "access_denied":
        return HTTPException(status_code=403, detail="access denied")
    if exc.category in VALIDATION_CATEGORIES:
        return HTTPException(
            status_code=422,
            detail={"message": "classroom request rejected", "category": exc.category},
        )
    if exc.category in CONFLICT_CATEGORIES:
        return HTTPException(
            status_code=409,
            detail={"message": "classroom request conflicts", "category": exc.category},
        )
    return HTTPException(status_code=409, detail="classroom request could not be completed")


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id", str(uuid4()))


def register_classroom_routes(app: FastAPI, service: ClassroomService) -> None:
    @app.get("/v1/facilities", response_model=list[FacilityResponse])
    async def list_facilities(
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> list[FacilityResponse]:
        try:
            values = await service.list_facilities(context.principal)
        except ClassroomError as exc:
            raise _http(exc) from None
        return [
            FacilityResponse(
                facility_id=str(value.facility_id),
                name=value.name,
                timezone=value.timezone,
                status=value.status,
                can_administer=value.can_administer,
            )
            for value in values
        ]

    @app.get("/v1/classrooms", response_model=list[ClassroomResponse])
    async def list_classrooms(
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> list[ClassroomResponse]:
        try:
            return [_classroom(value) for value in await service.list_classrooms(context.principal)]
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post("/v1/classrooms", response_model=ClassroomResponse, status_code=201)
    async def create_classroom(
        payload: ClassroomCreateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ClassroomResponse:
        try:
            return _classroom(
                await service.create_classroom(
                    context.principal,
                    payload.facility_id,
                    payload.name,
                    payload.age_band_label,
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.get("/v1/classrooms/{classroom_id}", response_model=ClassroomResponse)
    async def get_classroom(
        classroom_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> ClassroomResponse:
        try:
            return _classroom(await service.get_classroom(context.principal, classroom_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.patch("/v1/classrooms/{classroom_id}", response_model=ClassroomResponse)
    async def update_classroom(
        classroom_id: UUID,
        payload: ClassroomUpdateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ClassroomResponse:
        try:
            return _classroom(
                await service.update_classroom(
                    context.principal,
                    classroom_id,
                    _request_id(request),
                    name=payload.name,
                    age_band_label=payload.age_band_label,
                    set_age_band="age_band_label" in payload.model_fields_set,
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post("/v1/classrooms/{classroom_id}/activate", response_model=ClassroomResponse)
    async def activate_classroom(
        classroom_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ClassroomResponse:
        try:
            return _classroom(
                await service.set_classroom_active(
                    context.principal, classroom_id, True, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post("/v1/classrooms/{classroom_id}/deactivate", response_model=ClassroomResponse)
    async def deactivate_classroom(
        classroom_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ClassroomResponse:
        try:
            return _classroom(
                await service.set_classroom_active(
                    context.principal, classroom_id, False, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(
        "/v1/classrooms/{classroom_id}/ratio-policies",
        response_model=ClassroomResponse,
        status_code=201,
    )
    async def create_policy(
        classroom_id: UUID,
        payload: PolicyRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ClassroomResponse:
        try:
            return _classroom(
                await service.create_policy(
                    context.principal, classroom_id, payload.to_input(), _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.patch(
        "/v1/classrooms/{classroom_id}/ratio-policies/{policy_id}",
        response_model=ClassroomResponse,
    )
    async def update_policy(
        classroom_id: UUID,
        policy_id: UUID,
        payload: PolicyRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ClassroomResponse:
        try:
            return _classroom(
                await service.update_policy(
                    context.principal,
                    classroom_id,
                    policy_id,
                    payload.to_input(),
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(
        "/v1/classrooms/{classroom_id}/ratio-policies/{policy_id}/deactivate",
        response_model=ClassroomResponse,
    )
    async def deactivate_policy(
        classroom_id: UUID,
        policy_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_administer_facility)],
    ) -> ClassroomResponse:
        try:
            return _classroom(
                await service.deactivate_policy(
                    context.principal, classroom_id, policy_id, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.get("/v1/classrooms/{classroom_id}/ratio-status")
    async def ratio_status(
        classroom_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> dict[str, Any]:
        try:
            return (await service.ratio_status(context.principal, classroom_id)).as_dict()
        except ClassroomError as exc:
            raise _http(exc) from None
