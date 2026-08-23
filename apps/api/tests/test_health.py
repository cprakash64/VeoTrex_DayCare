from types import TracebackType

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from veotrex_api.config import Settings
from veotrex_api.main import create_app


class StubConnection:
    def __init__(self, failing: bool = False) -> None:
        self.failing = failing

    async def __aenter__(self) -> "StubConnection":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def execute(self, _statement: object) -> None:
        if self.failing:
            raise OperationalError("SELECT 1", {}, Exception("database unavailable"))


class StubEngine:
    def __init__(self, failing: bool = False) -> None:
        self.failing = failing
        self.disposed = False

    def connect(self) -> StubConnection:
        return StubConnection(self.failing)

    async def dispose(self) -> None:
        self.disposed = True


def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr("postgresql+psycopg://unused"),
        app_version="0.1.0-test",
    )


def test_liveness_returns_metadata_and_request_id() -> None:
    app = create_app(settings(), StubEngine())  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.get("/health/live", headers={"x-request-id": "request-123"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-123"
    assert response.json() == {
        "status": "ok",
        "service": "veotrex-api",
        "version": "0.1.0-test",
        "environment": "test",
    }


def test_readiness_reports_database_failure() -> None:
    app = create_app(settings(), StubEngine(failing=True))  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_readiness_reports_success() -> None:
    app = create_app(settings(), StubEngine())  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
