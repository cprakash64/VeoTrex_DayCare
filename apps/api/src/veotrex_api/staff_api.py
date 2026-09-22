"""HTTP surface of staff enrollment (V1-02A), registered on the application by ``main``.

Routes follow the existing conventions: bearer principal, permission dependency, tenant from
the identity mapping only, unknown and other-tenant identifiers both answer 404, bounded
generic error bodies. Templates have no route. Image bytes are served only as the canonical
JPEG to a principal of the owning tenant.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from veotrex_api.access import (
    AuthenticationFailureLogLimiter,
    PrincipalContext,
    require_permission,
)
from veotrex_api.authorization import Permission
from veotrex_api.staff_media import ALLOWED_MEDIA_TYPES, EnrollmentImageRejected
from veotrex_api.staff_service import (
    EnrollmentImageSummary,
    StaffEnrollmentService,
    StaffError,
    StaffSummary,
)

require_manage_staff = require_permission(Permission.MANAGE_STAFF)
require_read_operational = require_permission(Permission.READ_OPERATIONAL)

UPLOAD_PATH_PREFIX = "/v1/staff/"
UPLOAD_PATH_SUFFIX = "/enrollment-images"


def is_enrollment_upload(method: str, path: str) -> bool:
    """The one route that carries an image body, matched exactly for the body-limit guard."""
    if method != "POST" or not path.startswith(UPLOAD_PATH_PREFIX):
        return False
    if not path.endswith(UPLOAD_PATH_SUFFIX):
        return False
    middle = path[len(UPLOAD_PATH_PREFIX) : -len(UPLOAD_PATH_SUFFIX)]
    try:
        UUID(middle)
    except ValueError:
        return False
    return True


class StaffCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=200)


class StaffUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=200)


class StaffResponse(BaseModel):
    staff_id: str
    display_name: str
    status: str
    enrollment_state: str
    accepted_images: int
    required_images: int
    maximum_images: int
    recognition_ready: bool
    created_at: str
    updated_at: str


class EnrollmentImageResponse(BaseModel):
    image_id: str
    width: int
    height: int
    byte_size: int
    face_size_px: int | None
    quality: int | None
    template_state: str
    created_at: str


def _staff(value: StaffSummary) -> StaffResponse:
    return StaffResponse(
        staff_id=str(value.staff_id),
        display_name=value.display_name,
        status=value.status,
        enrollment_state=value.enrollment_state,
        accepted_images=value.accepted_images,
        required_images=value.required_images,
        maximum_images=value.maximum_images,
        recognition_ready=value.recognition_ready,
        created_at=value.created_at.isoformat(),
        updated_at=value.updated_at.isoformat(),
    )


def _image(value: EnrollmentImageSummary) -> EnrollmentImageResponse:
    return EnrollmentImageResponse(
        image_id=str(value.image_id),
        width=value.width,
        height=value.height,
        byte_size=value.byte_size,
        face_size_px=value.face_size_px,
        quality=value.quality,
        template_state=value.template_state,
        created_at=value.created_at.isoformat(),
    )


def _http(exc: StaffError) -> HTTPException:
    if exc.category == "not_found":
        return HTTPException(status_code=404, detail="staff profile not found")
    if exc.category == "access_denied":
        return HTTPException(status_code=403, detail="access denied")
    if exc.category in {"invalid_display_name", "staff_limit_reached"}:
        return HTTPException(
            status_code=422, detail={"message": "staff request rejected", "category": exc.category}
        )
    return HTTPException(status_code=409, detail="staff request could not be completed")


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id", str(uuid4()))


def register_staff_routes(
    app: FastAPI, service: StaffEnrollmentService, upload_limiter: AuthenticationFailureLogLimiter
) -> None:
    @app.get("/v1/staff", response_model=list[StaffResponse])
    async def list_staff(
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> list[StaffResponse]:
        try:
            return [_staff(value) for value in await service.list_profiles(context.principal)]
        except StaffError as exc:
            raise _http(exc) from None

    @app.post("/v1/staff", response_model=StaffResponse, status_code=status.HTTP_201_CREATED)
    async def create_staff(
        payload: StaffCreateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_staff)],
    ) -> StaffResponse:
        try:
            return _staff(
                await service.create_profile(
                    context.principal, payload.display_name, _request_id(request)
                )
            )
        except StaffError as exc:
            raise _http(exc) from None

    @app.get("/v1/staff/{staff_id}", response_model=StaffResponse)
    async def get_staff(
        staff_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> StaffResponse:
        try:
            return _staff(await service.get_profile(context.principal, staff_id))
        except StaffError as exc:
            raise _http(exc) from None

    @app.patch("/v1/staff/{staff_id}", response_model=StaffResponse)
    async def update_staff(
        staff_id: UUID,
        payload: StaffUpdateRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_staff)],
    ) -> StaffResponse:
        try:
            return _staff(
                await service.rename_profile(
                    context.principal, staff_id, payload.display_name, _request_id(request)
                )
            )
        except StaffError as exc:
            raise _http(exc) from None

    @app.post("/v1/staff/{staff_id}/activate", response_model=StaffResponse)
    async def activate_staff(
        staff_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_staff)],
    ) -> StaffResponse:
        try:
            return _staff(
                await service.set_active(context.principal, staff_id, True, _request_id(request))
            )
        except StaffError as exc:
            raise _http(exc) from None

    @app.post("/v1/staff/{staff_id}/deactivate", response_model=StaffResponse)
    async def deactivate_staff(
        staff_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_staff)],
    ) -> StaffResponse:
        try:
            return _staff(
                await service.set_active(context.principal, staff_id, False, _request_id(request))
            )
        except StaffError as exc:
            raise _http(exc) from None

    @app.delete("/v1/staff/{staff_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_staff(
        staff_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_staff)],
    ) -> Response:
        try:
            await service.delete_profile(context.principal, staff_id, _request_id(request))
        except StaffError as exc:
            raise _http(exc) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/staff/{staff_id}/enrollment-images", response_model=list[EnrollmentImageResponse])
    async def list_images(
        staff_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> list[EnrollmentImageResponse]:
        try:
            return [
                _image(value) for value in await service.list_images(context.principal, staff_id)
            ]
        except StaffError as exc:
            raise _http(exc) from None

    @app.post(
        "/v1/staff/{staff_id}/enrollment-images",
        response_model=EnrollmentImageResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_image(
        staff_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_staff)],
    ) -> EnrollmentImageResponse:
        # The body was bounded and its media type allow-listed by the request middleware.
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise HTTPException(status_code=415, detail="image/jpeg or image/png required")
        if not upload_limiter.allow():
            raise HTTPException(status_code=429, detail="upload limit exceeded")
        data = await request.body()
        try:
            return _image(
                await service.add_image(context.principal, staff_id, data, _request_id(request))
            )
        except EnrollmentImageRejected as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": "enrollment image rejected", "category": exc.category},
            ) from None
        except StaffError as exc:
            raise _http(exc) from None

    @app.get("/v1/staff/{staff_id}/enrollment-images/{image_id}/content")
    async def image_content(
        staff_id: UUID,
        image_id: UUID,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> Response:
        try:
            data = await service.image_content(context.principal, staff_id, image_id)
        except StaffError as exc:
            raise _http(exc) from None
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline",
            },
        )

    @app.delete(
        "/v1/staff/{staff_id}/enrollment-images/{image_id}",
        response_model=StaffResponse,
    )
    async def delete_image(
        staff_id: UUID,
        image_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_staff)],
    ) -> StaffResponse:
        try:
            return _staff(
                await service.remove_image(
                    context.principal, staff_id, image_id, _request_id(request)
                )
            )
        except StaffError as exc:
            raise _http(exc) from None
