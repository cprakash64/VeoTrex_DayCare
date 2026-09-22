from types import TracebackType

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from veotrex_api.config import Settings
from veotrex_api.db import PrivilegedDatabaseRole
from veotrex_api.main import create_app


class StubResult:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row

    def one(self) -> tuple[object, ...]:
        return self.row


class StubConnection:
    def __init__(
        self,
        failing: bool = False,
        role: tuple[object, ...] = ("veotrex_api", False, False, False, 0, 0),
    ) -> None:
        self.failing = failing
        self.role = role

    async def __aenter__(self) -> "StubConnection":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def execute(self, _statement: object) -> StubResult:
        if self.failing:
            raise OperationalError("SELECT 1", {}, Exception("database unavailable"))
        return StubResult(self.role)


class StubEngine:
    def __init__(
        self,
        failing: bool = False,
        role: tuple[object, ...] = ("veotrex_api", False, False, False, 0, 0),
    ) -> None:
        self.failing = failing
        self.role = role
        self.disposed = False

    def connect(self) -> StubConnection:
        return StubConnection(self.failing, self.role)

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
    assert response.json()["reason"] == "database_unavailable"


def test_readiness_reports_success() -> None:
    app = create_app(settings(), StubEngine())  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert "reason" not in response.json()


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (("veotrex", True, False, False, 0, 0), "SUPERUSER"),
        (("bypasser", False, True, False, 0, 0), "BYPASSRLS"),
        (("both", True, True, False, 0, 0), "SUPERUSER and BYPASSRLS"),
        (("creator", False, False, True, 0, 0), "CREATE on the application schema"),
        (("owner", False, False, False, 3, 0), "ownership of 3 application relation(s)"),
        (("member", False, False, False, 0, 1), "membership in 1 other role(s)"),
    ],
    ids=["superuser", "bypassrls", "superuser-and-bypassrls", "schema-create", "owner", "member"],
)
def test_privileged_database_role_refuses_startup(role: tuple[object, ...], expected: str) -> None:
    """A role RLS would not constrain, or one that could escape the model through ownership,
    schema CREATE or role membership, is a configuration fault: the process must not start.

    The failure names the role and the violated invariant so an operator can act on it, and
    never the DSN.
    """
    app = create_app(settings(), StubEngine(role=role))  # type: ignore[arg-type]
    with pytest.raises(PrivilegedDatabaseRole) as raised, TestClient(app):
        pass
    message = str(raised.value)
    offending = message.split(" has ", 1)[1].split(";", 1)[0]
    assert f"database role {role[0]!r}" in message
    assert offending == expected
    assert "postgresql" not in message


def test_privileged_database_role_fails_readiness_when_reached_after_startup() -> None:
    """If the database was unreachable at startup (deferred check) and later answers as a
    privileged role, readiness must fail loudly rather than serve traffic."""
    engine = StubEngine(failing=True)
    app = create_app(settings(), engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        engine.failing = False
        engine.role = ("veotrex", True, False, False, 0, 0)
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["reason"] == "privileged_database_role"
    assert "postgresql" not in response.text
