"""HTTP surface for camera portal configuration (V1-05A), registered by ``main``.

Human routes only (Auth0 operator principal); nothing here lives under ``/v1/edge`` and an edge
machine credential opens none of it - the edge does not fetch portals in this stage. Uniform
404 for unknown, other-tenant or not-in-this-classroom cameras; bounded error categories.
Requests carry numbers, a side and a label - never an image, a track or an identity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from veotrex_api.access import PrincipalContext, require_permission
from veotrex_api.authorization import Permission
from veotrex_api.camera_portal import DEFAULT_DEADBAND
from veotrex_api.camera_portal_service import (
    PORTAL_CONFLICT_CATEGORIES,
    PORTAL_VALIDATION_CATEGORIES,
    CameraPortals,
    CameraPortalService,
    PortalChange,
    PortalInput,
)
from veotrex_api.classroom_service import ClassroomError

require_read_operational = require_permission(Permission.READ_OPERATIONAL)
require_configure_cameras = require_permission(Permission.CONFIGURE_FACILITY_CAMERAS)


class PortalCreateRequest(BaseModel):
    """Normalised endpoints, the room's side as seen on the picture, a label. Nothing else."""

    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    x1: float = Field(strict=True)
    y1: float = Field(strict=True)
    x2: float = Field(strict=True)
    y2: float = Field(strict=True)
    inside_side: str = Field(min_length=1, max_length=8)
    deadband: float = Field(default=DEFAULT_DEADBAND, strict=True)
    enabled: bool = Field(default=True, strict=True)


class PortalUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=80)
    x1: float | None = Field(default=None, strict=True)
    y1: float | None = Field(default=None, strict=True)
    x2: float | None = Field(default=None, strict=True)
    y2: float | None = Field(default=None, strict=True)
    inside_side: str | None = Field(default=None, min_length=1, max_length=8)
    deadband: float | None = Field(default=None, strict=True)
    enabled: bool | None = Field(default=None, strict=True)


class PortalResponse(BaseModel):
    portal_id: str
    label: str
    x1: float
    y1: float
    x2: float
    y2: float
    inside_side: str
    inside_normal: list[float]
    deadband: float
    enabled: bool
    status: str
    revision: int
    edge_flag: str
    created_at: str
    updated_at: str
    archived_at: str | None


class CameraPortalsResponse(BaseModel):
    classroom_id: str
    classroom_name: str
    camera_id: str
    camera_name: str
    camera_status: str
    can_configure: bool
    max_portals: int
    edge_distribution: str
    portals: list[PortalResponse]


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _response(value: CameraPortals) -> CameraPortalsResponse:
    return CameraPortalsResponse(
        classroom_id=str(value.classroom_id),
        classroom_name=value.classroom_name,
        camera_id=str(value.camera_id),
        camera_name=value.camera_name,
        camera_status=value.camera_status,
        can_configure=value.can_configure,
        max_portals=value.max_portals,
        edge_distribution=value.edge_distribution,
        portals=[
            PortalResponse(
                portal_id=str(item.portal_id),
                label=item.label,
                x1=item.x1,
                y1=item.y1,
                x2=item.x2,
                y2=item.y2,
                inside_side=item.inside_side,
                inside_normal=list(item.inside_normal),
                deadband=item.deadband,
                enabled=item.enabled,
                status=item.status,
                revision=item.revision,
                edge_flag=item.edge_flag,
                created_at=item.created_at.isoformat(),
                updated_at=item.updated_at.isoformat(),
                archived_at=_iso(item.archived_at),
            )
            for item in value.portals
        ],
    )


def _http(exc: ClassroomError) -> HTTPException:
    if exc.category == "not_found":
        return HTTPException(status_code=404, detail="not found")
    if exc.category == "access_denied":
        return HTTPException(status_code=403, detail="access denied")
    if exc.category in PORTAL_VALIDATION_CATEGORIES:
        return HTTPException(
            status_code=422,
            detail={"message": "portal request rejected", "category": exc.category},
        )
    if exc.category in PORTAL_CONFLICT_CATEGORIES:
        return HTTPException(
            status_code=409,
            detail={"message": "portal request conflicts", "category": exc.category},
        )
    return HTTPException(status_code=409, detail="portal request could not be completed")


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id", str(uuid4()))


def register_camera_portal_routes(app: FastAPI, service: CameraPortalService) -> None:
    base = "/v1/classrooms/{classroom_id}/cameras/{camera_id}/portals"

    @app.get(base, response_model=CameraPortalsResponse)
    async def list_portals(
        classroom_id: UUID,
        camera_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> CameraPortalsResponse:
        try:
            return _response(await service.list_portals(context.principal, classroom_id, camera_id))
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(base, response_model=CameraPortalsResponse, status_code=201)
    async def create_portal(
        classroom_id: UUID,
        camera_id: UUID,
        payload: PortalCreateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_configure_cameras)],
    ) -> CameraPortalsResponse:
        try:
            return _response(
                await service.create_portal(
                    context.principal,
                    classroom_id,
                    camera_id,
                    PortalInput(
                        label=payload.label,
                        x1=payload.x1,
                        y1=payload.y1,
                        x2=payload.x2,
                        y2=payload.y2,
                        inside_side=payload.inside_side,
                        deadband=payload.deadband,
                        enabled=payload.enabled,
                    ),
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.patch(base + "/{portal_id}", response_model=CameraPortalsResponse)
    async def update_portal(
        classroom_id: UUID,
        camera_id: UUID,
        portal_id: UUID,
        payload: PortalUpdateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_configure_cameras)],
    ) -> CameraPortalsResponse:
        try:
            return _response(
                await service.update_portal(
                    context.principal,
                    classroom_id,
                    camera_id,
                    portal_id,
                    PortalChange(**payload.model_dump(exclude_unset=True)),
                    _request_id(request),
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None

    @app.post(base + "/{portal_id}/archive", response_model=CameraPortalsResponse)
    async def archive_portal(
        classroom_id: UUID,
        camera_id: UUID,
        portal_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_configure_cameras)],
    ) -> CameraPortalsResponse:
        try:
            return _response(
                await service.archive_portal(
                    context.principal, classroom_id, camera_id, portal_id, _request_id(request)
                )
            )
        except ClassroomError as exc:
            raise _http(exc) from None
