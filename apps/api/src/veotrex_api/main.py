import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from veotrex_api.access import (
    AuthenticationFailureLogLimiter,
    PrincipalContext,
    require_permission,
)
from veotrex_api.authorization import Permission
from veotrex_api.classroom_api import register_classroom_routes
from veotrex_api.classroom_service import ClassroomService
from veotrex_api.config import Settings, get_settings
from veotrex_api.credential_vault import (
    CredentialVault,
    InMemoryCredentialVault,
    UnavailableCredentialVault,
)
from veotrex_api.db import (
    PrivilegedDatabaseRole,
    inspect_role,
    make_engine,
    make_session_factory,
    require_unprivileged,
    verify_runtime_role,
)
from veotrex_api.edge_api import is_edge_whep_offer, register_edge_routes
from veotrex_api.edge_auth import EdgeAuthenticator
from veotrex_api.edge_whep import EdgeWhepBroker, WhepLeaseRegistry
from veotrex_api.encrypted_vault import (
    EncryptedCredentialVault,
    VaultKeyProvider,
)
from veotrex_api.face_backend import (
    FaceEnrollmentBackend,
    backend_for,
    supports_recognition,
)
from veotrex_api.identity import Auth0IdentityVerifier, IdentityVerifier
from veotrex_api.logging import configure_logging
from veotrex_api.request_media import (
    JSON_MEDIA_TYPE,
    TOKEN_EXCHANGE_MEDIA_TYPES,
    MalformedBody,
    UnsupportedMedia,
    canonical_code_payload,
    normalize_media_type,
)
from veotrex_api.ring_client import (
    SDP_MEDIA_TYPE,
    RingAmbiguousResult,
    RingClient,
    RingClientError,
)
from veotrex_api.ring_inventory_service import RingInventoryError, RingInventoryService
from veotrex_api.ring_service import RingLinkError, RingLinkService
from veotrex_api.ring_webhook import RingWebhookError, RingWebhookService
from veotrex_api.secrets import DefaultSecretResolver, SecretResolver
from veotrex_api.staff_api import (
    is_enrollment_upload,
    register_recognition_test_route,
    register_staff_routes,
)
from veotrex_api.staff_media import ALLOWED_MEDIA_TYPES, StaffMediaStore
from veotrex_api.staff_recognition import StaffRecognitionService
from veotrex_api.staff_roster_api import register_staff_roster_routes
from veotrex_api.staff_roster_service import StaffRosterService
from veotrex_api.staff_service import StaffEnrollmentService

require_read_operational = require_permission(Permission.READ_OPERATIONAL)
require_manage_integrations = require_permission(Permission.MANAGE_INTEGRATIONS)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str
    # Present only when not ready: which dependency or invariant failed. Never a DSN.
    reason: str | None = None


class RoleSummary(BaseModel):
    role: str
    facility_id: str | None


class MeResponse(BaseModel):
    actor_id: str
    tenant_id: str
    display_name: str | None
    roles: list[RoleSummary]
    permissions: list[str]


class RingTokenExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr = Field(min_length=1, max_length=2048)


class RingClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nonce: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")
    time: int = Field(ge=0)


class RingStateResponse(BaseModel):
    status: str
    connection_id: str | None = None


class RingLinkContextResponse(BaseModel):
    tenant_name: str
    eligible: bool


class RingSyncResponse(BaseModel):
    connection_id: str
    devices_seen: int
    cameras_created: int
    synchronized_at: str


class RingConnectionResponse(BaseModel):
    """Lifecycle and sync state of one Ring connection. No credential or account fields."""

    connection_id: str
    display_name: str
    provider: str = "RING"
    status: str
    integration_state: str
    operational_health: str
    last_synchronized_at: str | None
    last_sync_failure_category: str | None


class RingInventoryCameraResponse(BaseModel):
    camera_id: str
    connection_id: str
    display_name: str
    provider: str = "RING"
    inventory_state: str
    provider_online: bool | None
    capabilities: list[str]
    assigned: bool
    privacy_controls_configured: bool
    last_synchronized_at: str | None


class RingWebhookResponse(BaseModel):
    accepted: bool
    duplicate: bool


def _rewrite_as_json(request: Request, body: bytes) -> None:
    """Present an already-read, normalised body to FastAPI as JSON.

    The provider's media type has been checked against the endpoint's allowlist and its body
    converted to the canonical payload; rewriting the scope headers lets the unchanged route
    model remain the authority on what a valid authorization code is.
    """
    headers = [
        (name, value)
        for name, value in request.scope["headers"]
        if name.lower() not in (b"content-type", b"content-length")
    ]
    headers.append((b"content-type", JSON_MEDIA_TYPE.encode()))
    headers.append((b"content-length", str(len(body)).encode()))
    request.scope["headers"] = headers
    # Starlette caches Headers on first access; drop the cache so the rewrite is observable.
    request.__dict__.pop("_headers", None)
    request._body = body


def _default_vault(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    secrets: SecretResolver,
) -> CredentialVault:
    """Select the credential vault for this environment.

    Tests and local development keep the isolated in-memory adapter. Everywhere else the
    PostgreSQL-backed AEAD vault is used, and it stays fail-closed: without a usable master key the
    unavailable adapter is returned rather than starting with unprotected credential storage.
    """
    if settings.environment.lower() in {"test", "development", "local"}:
        return InMemoryCredentialVault()
    key_provider = VaultKeyProvider(secrets, settings.vault_master_key_ref)
    if not key_provider.available():
        return UnavailableCredentialVault()
    return EncryptedCredentialVault(session_factory, key_provider)


def _face_backend_for(settings: Settings) -> FaceEnrollmentBackend:
    """Select the configured face backend.

    ``opencv_eval`` produces real adult biometric templates, so it is built through
    ``face_opencv.build``, which refuses a forbidden environment itself. That import is local
    to this branch: the control-plane image installs no OpenCV, and the module must not even
    be imported where the backend can never be selected.
    """
    if settings.staff_face_backend == "opencv_eval":
        from veotrex_api.face_opencv import build

        return build(settings)
    return backend_for(settings.staff_face_backend)


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    identity_verifier: IdentityVerifier | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    credential_vault: CredentialVault | None = None,
    ring_client: RingClient | None = None,
    secret_resolver: SecretResolver | None = None,
    face_backend: FaceEnrollmentBackend | None = None,
    staff_media: StaffMediaStore | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    resolved_engine = engine or make_engine(resolved_settings)
    logger = structlog.get_logger()

    resolved_factory = session_factory or make_session_factory(resolved_engine)
    resolved_secrets = secret_resolver or DefaultSecretResolver()
    resolved_vault = credential_vault or _default_vault(
        resolved_settings, resolved_factory, resolved_secrets
    )
    resolved_ring_client = ring_client or RingClient(resolved_settings, resolved_secrets)
    ring_service = RingLinkService(
        resolved_settings,
        resolved_factory,
        resolved_vault,
        resolved_ring_client,
        resolved_secrets,
    )
    inventory_service = RingInventoryService(resolved_factory, ring_service, resolved_ring_client)
    webhook_service = RingWebhookService(
        resolved_factory,
        resolved_secrets,
        resolved_settings.ring_hmac_signing_key_ref,
        resolved_vault,
        inventory_service,
    )
    # Edge WHEP broker (V1-DEMO-03B). The lease registry is process-local and bounded; it is
    # correct only because the API runs as a single worker process (ADR 0021).
    edge_whep_broker = EdgeWhepBroker(
        resolved_settings,
        resolved_factory,
        ring_service,
        resolved_ring_client,
        WhepLeaseRegistry(
            max_active=resolved_settings.edge_whep_max_active_leases,
            max_per_node=resolved_settings.edge_whep_max_leases_per_node,
            ttl_seconds=resolved_settings.edge_whep_lease_ttl_seconds,
        ),
    )
    resolved_media = staff_media or StaffMediaStore(Path(resolved_settings.staff_media_dir))
    resolved_face_backend = face_backend or _face_backend_for(resolved_settings)
    staff_service = StaffEnrollmentService(
        resolved_factory,
        resolved_media,
        resolved_face_backend,
        max_image_bytes=resolved_settings.staff_enrollment_image_bytes,
    )
    # EVALUATION ONLY (V1-02B0). Built only where settings permit it, and only when the backend
    # in use can actually recognise; staging and production reach neither condition, so no
    # recognition service exists there and no route is registered for one.
    recognition_service: StaffRecognitionService | None = None
    if resolved_settings.face_evaluation_permitted and supports_recognition(resolved_face_backend):
        recognition_service = StaffRecognitionService(
            resolved_factory,
            resolved_face_backend,
            max_image_bytes=resolved_settings.staff_enrollment_image_bytes,
            threshold=resolved_settings.staff_recognition_threshold,
            margin=resolved_settings.staff_recognition_margin,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Tenant isolation is PostgreSQL Row Level Security, which a superuser or BYPASSRLS
        # role is exempt from. Such a connection is a configuration fault: refuse to start.
        # An unreachable database is an availability condition instead; readiness keeps
        # re-checking the role and stays not-ready until it can prove the boundary.
        try:
            identity = await verify_runtime_role(resolved_engine)
        except PrivilegedDatabaseRole as exc:
            logger.error(
                "database_role_privileged",
                role=exc.identity.role,
                violations=list(exc.identity.violations),
            )
            raise
        except SQLAlchemyError:
            logger.warning("database_role_check_deferred", dependency="database")
        else:
            logger.info("database_role_verified", role=identity.role)
        try:
            resolved_media.ensure_ready()
        except OSError:
            logger.error("staff_media_dir_unavailable")
            raise
        # A backend with weights to verify loads them here, so a bad digest, a missing file or
        # an environment that must not have real recognition stops the process at startup
        # rather than at the first upload.
        preparing = getattr(resolved_face_backend, "ensure_ready", None)
        if callable(preparing):
            try:
                preparing()
            except Exception:
                logger.error(
                    "face_backend_unavailable",
                    backend=resolved_settings.staff_face_backend,
                )
                raise
        logger.info(
            "service_started",
            service=resolved_settings.service_name,
            environment=resolved_settings.environment,
            version=resolved_settings.app_version,
        )
        expiry = asyncio.create_task(edge_whep_broker.run_expiry_loop())
        yield
        expiry.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await expiry
        # Outstanding Ring sessions are torn down while the Ring client is still open.
        await edge_whep_broker.shutdown()
        await resolved_ring_client.aclose()
        await resolved_engine.dispose()
        logger.info("service_stopped", service=resolved_settings.service_name)

    app = FastAPI(title="VeoTrex API", version=resolved_settings.app_version, lifespan=lifespan)
    app.state.engine = resolved_engine
    app.state.settings = resolved_settings
    app.state.identity_verifier = identity_verifier or Auth0IdentityVerifier(resolved_settings)
    app.state.session_factory = resolved_factory
    app.state.ring_service = ring_service
    app.state.ring_inventory_service = inventory_service
    app.state.ring_webhook_service = webhook_service
    app.state.ring_token_exchange_limiter = AuthenticationFailureLogLimiter(
        limit=resolved_settings.ring_token_exchange_rate_limit_per_minute,
        window_seconds=60,
    )
    app.state.staff_service = staff_service
    app.state.edge_authenticator = EdgeAuthenticator(
        resolved_factory, AuthenticationFailureLogLimiter()
    )
    app.state.edge_whep_broker = edge_whep_broker
    register_edge_routes(
        app,
        edge_whep_broker,
        AuthenticationFailureLogLimiter(
            limit=resolved_settings.edge_whep_rate_limit_per_minute, window_seconds=60
        ),
    )
    app.state.staff_recognition_service = recognition_service
    register_staff_routes(
        app,
        staff_service,
        AuthenticationFailureLogLimiter(
            limit=resolved_settings.staff_enrollment_upload_rate_limit_per_minute,
            window_seconds=60,
        ),
    )
    # Classrooms and configured ratio policies (V1-04A).
    app.state.classroom_service = ClassroomService(resolved_factory)
    register_classroom_routes(app, app.state.classroom_service)
    # Facility staff roster, ratio eligibility and operator staff check-in/out (V1-04C). Always
    # registered and independent of the face backend: presence is recorded by an operator, never
    # by recognition.
    app.state.staff_roster_service = StaffRosterService(resolved_factory)
    register_staff_roster_routes(app, app.state.staff_roster_service)
    if recognition_service is not None:
        register_recognition_test_route(
            app,
            recognition_service,
            AuthenticationFailureLogLimiter(
                limit=resolved_settings.staff_enrollment_upload_rate_limit_per_minute,
                window_seconds=60,
            ),
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            body_limit: int | None = None
            media_type = ""
            is_token_exchange = request.url.path == "/v1/integrations/ring/token-exchange"
            is_enrollment_image = is_enrollment_upload(request.method, request.url.path)
            is_edge_offer = is_edge_whep_offer(request.method, request.url.path)
            if is_token_exchange:
                body_limit = resolved_settings.ring_token_exchange_body_bytes
            elif request.url.path == "/v1/providers/ring/webhooks":
                body_limit = resolved_settings.ring_webhook_body_bytes
            elif is_enrollment_image:
                body_limit = resolved_settings.staff_enrollment_image_bytes
            elif is_edge_offer:
                # Refused before authentication or forwarding: an oversized offer never
                # reaches the broker, let alone Ring.
                body_limit = resolved_settings.edge_whep_max_offer_bytes
            if body_limit is not None:
                media_type = normalize_media_type(request.headers.get("content-type"))

                def observed(status_code: int) -> Response:
                    """Record transport facts for a refused provider request, never its body."""
                    logger.info(
                        "provider_request",
                        path=request.url.path,
                        media_type=media_type,
                        content_length=request.headers.get("content-length"),
                        user_agent=request.headers.get("user-agent", "")[:120],
                        status_code=status_code,
                    )
                    return Response(status_code=status_code)

                if request.method != "POST":
                    return observed(status.HTTP_405_METHOD_NOT_ALLOWED)
                # A closed allowlist per endpoint. The webhook contract is unchanged; only the
                # token exchange accepts the shapes Ring's Java client actually sends.
                if is_token_exchange:
                    accepted = TOKEN_EXCHANGE_MEDIA_TYPES
                elif is_enrollment_image:
                    accepted = ALLOWED_MEDIA_TYPES
                elif is_edge_offer:
                    accepted = frozenset({SDP_MEDIA_TYPE})
                else:
                    accepted = frozenset({"application/json"})
                if media_type not in accepted:
                    return observed(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
                raw_length = request.headers.get("content-length")
                if raw_length is not None:
                    try:
                        declared_length = int(raw_length)
                    except ValueError:
                        return observed(status.HTTP_400_BAD_REQUEST)
                    if declared_length < 0:
                        return observed(status.HTTP_400_BAD_REQUEST)
                    if declared_length > body_limit:
                        return observed(status.HTTP_413_CONTENT_TOO_LARGE)
                bounded_body = bytearray()
                async for chunk in request.stream():
                    bounded_body.extend(chunk)
                    if len(bounded_body) > body_limit:
                        return observed(status.HTTP_413_CONTENT_TOO_LARGE)
                if is_token_exchange and media_type != JSON_MEDIA_TYPE:
                    # Only the shapes FastAPI cannot parse are converted. A JSON body is
                    # left exactly as sent so RingTokenExchangeRequest keeps validating it,
                    # extra="forbid" included: nothing observed from Ring justifies
                    # loosening a deliberate contract.
                    try:
                        canonical = canonical_code_payload(media_type, bytes(bounded_body))
                    except UnsupportedMedia:
                        return observed(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
                    except MalformedBody:
                        return observed(status.HTTP_422_UNPROCESSABLE_CONTENT)
                    _rewrite_as_json(request, canonical)
                else:
                    request._body = bytes(bounded_body)
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            if body_limit is not None:
                logger.info(
                    "provider_request",
                    path=request.url.path,
                    media_type=media_type,
                    content_length=request.headers.get("content-length"),
                    user_agent=request.headers.get("user-agent", "")[:120],
                    status_code=response.status_code,
                )
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    @app.get("/health/live", response_model=HealthResponse, response_model_exclude_none=True)
    async def live() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=resolved_settings.service_name,
            version=resolved_settings.app_version,
            environment=resolved_settings.environment,
        )

    @app.get("/health/ready", response_model=HealthResponse, response_model_exclude_none=True)
    async def ready(response: Response) -> HealthResponse:
        # Readiness proves two things on every call: the database answers, and the role it
        # answers as is subject to Row Level Security. A privileged role is reported as its own
        # reason so an orchestrator's health gate fails the rollout rather than serving traffic.
        health_status = "ready"
        reason: str | None = None
        try:
            async with resolved_engine.connect() as connection:
                require_unprivileged(await inspect_role(connection))
        except PrivilegedDatabaseRole as exc:
            health_status = "not_ready"
            reason = "privileged_database_role"
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            logger.error(
                "readiness_check_failed",
                dependency="database",
                reason=reason,
                role=exc.identity.role,
                violations=list(exc.identity.violations),
            )
        except SQLAlchemyError:
            health_status = "not_ready"
            reason = "database_unavailable"
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            logger.warning("readiness_check_failed", dependency="database", reason=reason)
        return HealthResponse(
            status=health_status,
            service=resolved_settings.service_name,
            version=resolved_settings.app_version,
            environment=resolved_settings.environment,
            reason=reason,
        )

    @app.get("/v1/me", response_model=MeResponse)
    async def me(
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> MeResponse:
        principal = context.principal
        return MeResponse(
            actor_id=str(principal.actor_id),
            tenant_id=str(principal.tenant_id),
            display_name=principal.display_name,
            roles=[
                RoleSummary(
                    role=grant.role.value,
                    facility_id=str(grant.facility_id) if grant.facility_id else None,
                )
                for grant in principal.grants
            ],
            permissions=sorted(permission.value for permission in principal.permissions),
        )

    @app.post("/v1/integrations/ring/token-exchange", response_model=RingStateResponse)
    async def ring_token_exchange(
        payload: RingTokenExchangeRequest, request: Request
    ) -> RingStateResponse:
        if not request.app.state.ring_token_exchange_limiter.allow():
            raise HTTPException(status_code=429, detail="request limit exceeded")
        service: RingLinkService = request.app.state.ring_service
        try:
            receipt = await service.receive_authorization_code(payload.code)
        except RingAmbiguousResult:
            raise HTTPException(status_code=503, detail="provider result uncertain") from None
        except RingClientError:
            raise HTTPException(status_code=502, detail="provider request failed") from None
        except RingLinkError:
            raise HTTPException(status_code=409, detail="account link unavailable") from None
        return RingStateResponse(status=receipt.value)

    @app.get("/v1/integrations/ring/link-context", response_model=RingLinkContextResponse)
    async def ring_link_context(
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_integrations)],
        nonce: str = Query(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$"),
        time: int = Query(ge=0),
    ) -> RingLinkContextResponse:
        service: RingLinkService = request.app.state.ring_service
        value = await service.link_context(context.principal, time, nonce)
        return RingLinkContextResponse(tenant_name=value.tenant_name, eligible=value.eligible)

    @app.post("/v1/integrations/ring/claim", response_model=RingStateResponse)
    async def ring_claim(
        payload: RingClaimRequest,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_integrations)],
    ) -> RingStateResponse:
        service: RingLinkService = request.app.state.ring_service
        try:
            result = await service.claim(
                context.principal,
                timestamp_ms=payload.time,
                nonce=payload.nonce,
                request_id=request.headers.get("x-request-id", str(uuid4())),
            )
        except RingLinkError as exc:
            code = 409 if exc.category in {"link_already_used", "connection_conflict"} else 422
            raise HTTPException(
                status_code=code, detail="Ring account could not be connected"
            ) from None
        return RingStateResponse(status=result.state.value, connection_id=str(result.connection_id))

    @app.post(
        "/v1/integrations/ring/connections/{connection_id}/resume",
        response_model=RingStateResponse,
    )
    async def ring_resume(
        connection_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_integrations)],
    ) -> RingStateResponse:
        service: RingLinkService = request.app.state.ring_service
        try:
            result = await service.resume_completion(
                context.principal,
                connection_id,
                request.headers.get("x-request-id", str(uuid4())),
            )
        except RingLinkError:
            raise HTTPException(
                status_code=409, detail="Ring completion remains unavailable"
            ) from None
        return RingStateResponse(status=result.state.value, connection_id=str(result.connection_id))

    @app.post(
        "/v1/integrations/ring/connections/{connection_id}/disconnect",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def ring_disconnect(
        connection_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_integrations)],
    ) -> Response:
        service: RingLinkService = request.app.state.ring_service
        try:
            await service.disconnect(
                context.principal,
                connection_id,
                request.headers.get("x-request-id", str(uuid4())),
            )
        except RingLinkError:
            raise HTTPException(status_code=404, detail="Ring connection not found") from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/integrations/ring/connections",
        response_model=list[RingConnectionResponse],
    )
    async def ring_connections(
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> list[RingConnectionResponse]:
        service: RingInventoryService = request.app.state.ring_inventory_service
        values = await service.list_connections(context.principal)
        return [
            RingConnectionResponse(
                connection_id=str(value.connection_id),
                display_name=value.display_name,
                status=value.status,
                integration_state=value.integration_state,
                operational_health=value.operational_health,
                last_synchronized_at=(
                    value.last_synchronized_at.isoformat()
                    if value.last_synchronized_at is not None
                    else None
                ),
                last_sync_failure_category=value.last_sync_failure_category,
            )
            for value in values
        ]

    @app.get(
        "/v1/integrations/ring/devices",
        response_model=list[RingInventoryCameraResponse],
    )
    async def ring_devices(
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_read_operational)],
    ) -> list[RingInventoryCameraResponse]:
        service: RingInventoryService = request.app.state.ring_inventory_service
        values = await service.list_inventory(context.principal)
        return [
            RingInventoryCameraResponse(
                camera_id=str(value.camera_id),
                connection_id=str(value.connection_id),
                display_name=value.display_name,
                inventory_state=value.inventory_state,
                provider_online=value.provider_online,
                capabilities=list(value.capabilities),
                assigned=value.assigned,
                privacy_controls_configured=value.privacy_controls_configured,
                last_synchronized_at=(
                    value.last_synchronized_at.isoformat()
                    if value.last_synchronized_at is not None
                    else None
                ),
            )
            for value in values
        ]

    @app.post(
        "/v1/integrations/ring/connections/{connection_id}/sync",
        response_model=RingSyncResponse,
    )
    async def ring_sync(
        connection_id: UUID,
        request: Request,
        context: Annotated[PrincipalContext, Depends(require_manage_integrations)],
    ) -> RingSyncResponse:
        service: RingInventoryService = request.app.state.ring_inventory_service
        try:
            result = await service.sync_connection(context.principal, connection_id)
        except RingInventoryError as exc:
            code = 404 if exc.category == "connection_unavailable" else 502
            raise HTTPException(
                status_code=code, detail="Ring synchronization unavailable"
            ) from None
        return RingSyncResponse(
            connection_id=str(result.connection_id),
            devices_seen=result.devices_seen,
            cameras_created=result.cameras_created,
            synchronized_at=result.synchronized_at.isoformat(),
        )

    @app.post("/v1/providers/ring/webhooks", response_model=RingWebhookResponse)
    async def ring_webhook(request: Request) -> RingWebhookResponse:
        service: RingWebhookService = request.app.state.ring_webhook_service
        try:
            inserted = await service.ingest(
                await request.body(), request.headers.get("x-signature")
            )
        except RingWebhookError as exc:
            code = 503 if exc.category == "verification_unavailable" else 401
            if exc.category in {
                "malformed_envelope",
                "malformed_component_ids",
                "malformed_event_timestamp",
                "malformed_sub_type",
                "malformed_source",
            }:
                code = 400
            elif exc.category == "unsupported_version":
                code = 422
            raise HTTPException(status_code=code, detail="Ring webhook rejected") from None
        return RingWebhookResponse(accepted=True, duplicate=not inserted)

    return app


app = create_app()
