from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from veotrex_api.config import Settings, get_settings
from veotrex_api.db import make_engine
from veotrex_api.logging import configure_logging


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


def create_app(settings: Settings | None = None, engine: AsyncEngine | None = None) -> FastAPI:
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

    return app


app = create_app()
