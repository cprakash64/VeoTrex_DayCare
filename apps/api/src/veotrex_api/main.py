from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from veotrex_api.access import (
    AuthenticationFailureLogLimiter,
    PrincipalContext,
    require_permission,
)
from veotrex_api.authorization import Permission
from veotrex_api.config import Settings, get_settings
from veotrex_api.credential_vault import (
    CredentialVault,
    InMemoryCredentialVault,
    UnavailableCredentialVault,
)
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.identity import Auth0IdentityVerifier, IdentityVerifier
from veotrex_api.logging import configure_logging
from veotrex_api.ring_client import RingAmbiguousResult, RingClient, RingClientError
from veotrex_api.ring_service import RingLinkError, RingLinkService
from veotrex_api.secrets import EnvironmentSecretResolver, SecretResolver

require_read_operational = require_permission(Permission.READ_OPERATIONAL)
require_manage_integrations = require_permission(Permission.MANAGE_INTEGRATIONS)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


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


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    identity_verifier: IdentityVerifier | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    credential_vault: CredentialVault | None = None,
    ring_client: RingClient | None = None,
    secret_resolver: SecretResolver | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    resolved_engine = engine or make_engine(resolved_settings)
    logger = structlog.get_logger()

    resolved_factory = session_factory or make_session_factory(resolved_engine)
    resolved_secrets = secret_resolver or EnvironmentSecretResolver()
    resolved_vault = credential_vault or (
        InMemoryCredentialVault()
        if resolved_settings.environment.lower() in {"test", "development", "local"}
        else UnavailableCredentialVault()
    )
    resolved_ring_client = ring_client or RingClient(resolved_settings, resolved_secrets)
    ring_service = RingLinkService(
        resolved_settings,
        resolved_factory,
        resolved_vault,
        resolved_ring_client,
        resolved_secrets,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "service_started",
            service=resolved_settings.service_name,
            environment=resolved_settings.environment,
            version=resolved_settings.app_version,
        )
        yield
        await resolved_ring_client.aclose()
        await resolved_engine.dispose()
        logger.info("service_stopped", service=resolved_settings.service_name)

    app = FastAPI(title="VeoTrex API", version=resolved_settings.app_version, lifespan=lifespan)
    app.state.engine = resolved_engine
    app.state.settings = resolved_settings
    app.state.identity_verifier = identity_verifier or Auth0IdentityVerifier(resolved_settings)
    app.state.session_factory = resolved_factory
    app.state.ring_service = ring_service
    app.state.ring_token_exchange_limiter = AuthenticationFailureLogLimiter(
        limit=resolved_settings.ring_token_exchange_rate_limit_per_minute,
        window_seconds=60,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            if request.url.path == "/v1/integrations/ring/token-exchange":
                if request.method != "POST":
                    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)
                content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
                if content_type != "application/json":
                    return Response(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
                raw_length = request.headers.get("content-length")
                if raw_length is not None:
                    try:
                        declared_length = int(raw_length)
                    except ValueError:
                        return Response(status_code=status.HTTP_400_BAD_REQUEST)
                    if declared_length < 0:
                        return Response(status_code=status.HTTP_400_BAD_REQUEST)
                    if declared_length > resolved_settings.ring_token_exchange_body_bytes:
                        return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
                bounded_body = bytearray()
                async for chunk in request.stream():
                    bounded_body.extend(chunk)
                    if len(bounded_body) > resolved_settings.ring_token_exchange_body_bytes:
                        return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
                request._body = bytes(bounded_body)
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    @app.get("/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=resolved_settings.service_name,
            version=resolved_settings.app_version,
            environment=resolved_settings.environment,
        )

    @app.get("/health/ready", response_model=HealthResponse)
    async def ready(response: Response) -> HealthResponse:
        health_status = "ready"
        try:
            async with resolved_engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            health_status = "not_ready"
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            logger.warning("readiness_check_failed", dependency="database")
        return HealthResponse(
            status=health_status,
            service=resolved_settings.service_name,
            version=resolved_settings.app_version,
            environment=resolved_settings.environment,
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

    return app


app = create_app()
