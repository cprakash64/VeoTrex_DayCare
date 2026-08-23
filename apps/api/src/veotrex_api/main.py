from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import structlog
from fastapi import Depends, FastAPI, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from veotrex_api.access import PrincipalContext, require_permission
from veotrex_api.authorization import Permission
from veotrex_api.config import Settings, get_settings
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.identity import Auth0IdentityVerifier, IdentityVerifier
from veotrex_api.logging import configure_logging

require_read_operational = require_permission(Permission.READ_OPERATIONAL)


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


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    identity_verifier: IdentityVerifier | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    resolved_engine = engine or make_engine(resolved_settings)
    logger = structlog.get_logger()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "service_started",
            service=resolved_settings.service_name,
            environment=resolved_settings.environment,
            version=resolved_settings.app_version,
        )
        yield
        await resolved_engine.dispose()
        logger.info("service_stopped", service=resolved_settings.service_name)

    app = FastAPI(title="VeoTrex API", version=resolved_settings.app_version, lifespan=lifespan)
    app.state.engine = resolved_engine
    app.state.settings = resolved_settings
    app.state.identity_verifier = identity_verifier or Auth0IdentityVerifier(resolved_settings)
    app.state.session_factory = session_factory or make_session_factory(resolved_engine)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
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

    return app


app = create_app()
