"""HTTP surface of staff enrollment (V1-02A), registered on the application by ``main``.

Routes follow the existing conventions: bearer principal, permission dependency, tenant from
the identity mapping only, unknown and other-tenant identifiers both answer 404, bounded
generic error bodies. Templates have no route. Image bytes are served only as the canonical
JPEG to a principal of the owning tenant.

V1-02B0 adds one evaluation-only route, ``POST /v1/staff/recognition-test``. It is registered
by ``register_recognition_test_route`` and ``main`` calls that only when settings permit it, so
in staging and production the path does not exist at all rather than existing and refusing -
there is nothing to misconfigure into life. It persists nothing and returns no embedding.
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
from veotrex_api.staff_recognition import StaffRecognitionService
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
RECOGNITION_TEST_PATH = "/v1/staff/recognition-test"


def is_enrollment_upload(method: str, path: str) -> bool:
    """A route that carries a raw image body, matched exactly for the body-limit guard.

    The recognition-test route is included: its body is an image of exactly the same shape and
    must be bounded and media-type checked by the same middleware, before any of it is read.
    When the route is not registered the guard still matches, and the request then 404s having
    been bounded - which is the safe order.
    """
    if method != "POST":
        return False
    if path == RECOGNITION_TEST_PATH:
        return True
    if not path.startswith(UPLOAD_PATH_PREFIX) or not path.endswith(UPLOAD_PATH_SUFFIX):
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


class RecognitionTestResponse(BaseModel):
    """Evaluation output. ``staff_id`` and ``display_name`` are present only on a MATCH, so an
    UNKNOWN answer cannot leak the closest candidate's identity; ``score`` is a bounded
    similarity, never an embedding."""

    decision: str
    staff_id: str | None = None
    display_name: str | None = None
    score: float
    runner_up_score: float | None = None
    reason: str | None = None
    candidates: int
    model_id: str
    model_version: str
    threshold: float
    margin: float
    evaluation_only: bool = True


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


def register_recognition_test_route(
    app: FastAPI,
    service: StaffRecognitionService,
    upload_limiter: AuthenticationFailureLogLimiter,
) -> None:
    """Register the local recognition-test route. EVALUATION ONLY.

    ``main`` calls this only when ``Settings.recognition_test_enabled`` is true, which requires
    both a permitted environment and a backend that can recognise. Declared before the
    ``/v1/staff/{staff_id}`` routes would match it is unnecessary - "recognition-test" is not a
    UUID, so the typed path parameter rejects it - but the literal path is registered here in
    full rather than nested under a staff id, because the query is about the tenant's whole
    roster and belongs to no single profile.
    """

    @app.post(RECOGNITION_TEST_PATH, response_model=RecognitionTestResponse)
    async def recognition_test(
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_staff)],
    ) -> RecognitionTestResponse:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise HTTPException(status_code=415, detail="image/jpeg or image/png required")
        if not upload_limiter.allow():
            raise HTTPException(status_code=429, detail="upload limit exceeded")
        data = await request.body()
        try:
            result = await service.recognize_image(context.principal, data)
        except EnrollmentImageRejected as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": "recognition image rejected", "category": exc.category},
            ) from None
        except StaffError as exc:
            raise _http(exc) from None
        finally:
            # The request body is not referenced past this point. Dropping the local name is
            # the only disposal Python can offer; the bytes are never written anywhere.
            del data
        return RecognitionTestResponse(
            decision=result.decision,
            staff_id=None if result.staff_id is None else str(result.staff_id),
            display_name=result.display_name,
            score=result.score,
            runner_up_score=result.runner_up_score,
            reason=result.reason,
            candidates=result.candidates,
            model_id=result.model_id,
            model_version=result.model_version,
            threshold=result.threshold,
            margin=result.margin,
        )
