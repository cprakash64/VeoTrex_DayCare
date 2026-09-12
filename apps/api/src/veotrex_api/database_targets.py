"""Deterministic separation of the development and automated-test PostgreSQL targets.

The database suite performs cluster-scoped operations (``CREATE ROLE``, ``DROP ROLE``,
``DROP OWNED``) as well as row deletion. PostgreSQL roles are **cluster-wide**, so isolating those
operations requires a separate PostgreSQL *server*, not merely a separate database inside the
development server: dropping a role in one database removes it for every database in that cluster.

Every check here is structural and deterministic - a recognised test database name, a loopback
host, and a cluster identity ``(host, port)`` distinct from the development target. No target is
ever accepted by default, and no password is parsed, retained, printed, or logged.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DEVELOPMENT_DATABASE_URL_ENV = "VEOTREX_DATABASE_URL"
TEST_DATABASE_URL_ENV = "VEOTREX_TEST_DATABASE_URL"

TEST_DATABASE_SUFFIX = "_test"
DEFAULT_POSTGRES_PORT = 5432

# Databases that belong to a development or system cluster and may never be a destructive
# test target, whatever else the URL claims.
RESERVED_DATABASES = frozenset({"veotrex", "postgres", "template0", "template1"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# A target the suite cannot reach: no credential, and nothing listens on port 1. Used when no
# test database is configured, so destructive tests fail to connect instead of silently falling
# back to the development database.
UNCONFIGURED_TEST_DATABASE_URL = (
    "postgresql+psycopg://veotrex_test@127.0.0.1:1/veotrex_test_unconfigured"
)


class DatabaseTargetError(ValueError):
    """A database target could not be accepted."""


class UnconfiguredTestDatabase(DatabaseTargetError):
    """No test database target is configured; destructive tests must not run."""


class UnsafeTestDatabase(DatabaseTargetError):
    """A test target was configured but is not demonstrably isolated from development."""


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    """Non-secret identity of a database target. Credentials are never stored here."""

    host: str
    port: int
    database: str

    @property
    def cluster(self) -> tuple[str, int]:
        """The isolation boundary for roles.

        Roles are cluster-wide, so two targets sharing ``(host, port)`` share every role even
        when their database names differ. This, not the database name, is the decisive check.
        """
        return (self.host, self.port)

    def describe(self) -> str:
        """Safe rendering for diagnostics: host, port and database only - never a credential."""
        return f"{self.host}:{self.port}/{self.database}"


def parse_database_url(url: str) -> DatabaseTarget:
    """Extract the non-secret target from a database URL, discarding any credential."""
    parts = urlsplit(url)
    if not parts.scheme.startswith("postgresql"):
        raise DatabaseTargetError("database URL must use a postgresql scheme")
    host = (parts.hostname or "").strip()
    if not host:
        raise DatabaseTargetError("database URL must name a host")
    database = parts.path.lstrip("/")
    if not database:
        raise DatabaseTargetError("database URL must name a database")
    return DatabaseTarget(host, parts.port or DEFAULT_POSTGRES_PORT, database)


def require_test_target(test_url: str | None, development_url: str | None = None) -> DatabaseTarget:
    """Validate a destructive-test target, or refuse.

    Raises :class:`UnconfiguredTestDatabase` when nothing is configured and
    :class:`UnsafeTestDatabase` when the configured target is not demonstrably isolated.
    """
    if not test_url:
        raise UnconfiguredTestDatabase(f"{TEST_DATABASE_URL_ENV} is not set")
    target = parse_database_url(test_url)
    if target.database in RESERVED_DATABASES:
        raise UnsafeTestDatabase("test target names a development or system database")
    if not target.database.endswith(TEST_DATABASE_SUFFIX):
        raise UnsafeTestDatabase(f"test database name must end in {TEST_DATABASE_SUFFIX!r}")
    if target.host not in LOOPBACK_HOSTS:
        raise UnsafeTestDatabase("test target host must be loopback for local qualification")
    if development_url:
        development = parse_database_url(development_url)
        if target.cluster == development.cluster:
            # Decisive: same server means shared roles, so DROP ROLE in the "test" database
            # would still affect development.
            raise UnsafeTestDatabase(
                "test target shares the development PostgreSQL cluster; roles are cluster-wide"
            )
    return target


def require_development_target(development_url: str | None) -> DatabaseTarget:
    """Validate a development migration target, refusing a test database."""
    if not development_url:
        raise DatabaseTargetError(f"{DEVELOPMENT_DATABASE_URL_ENV} is not set")
    target = parse_database_url(development_url)
    if target.database.endswith(TEST_DATABASE_SUFFIX):
        raise DatabaseTargetError("refusing to run a development migration against a test database")
    return target


def resolve_test_database_url(environ: Mapping[str, str] | None = None) -> str:
    """Return the validated test URL, or an unreachable sentinel when none is configured.

    An unsafe configured target raises: that is a mistake worth failing loudly. An absent one
    yields the sentinel so the suite fails to connect rather than reaching development data.
    """
    env = os.environ if environ is None else environ
    try:
        require_test_target(env.get(TEST_DATABASE_URL_ENV), env.get(DEVELOPMENT_DATABASE_URL_ENV))
    except UnconfiguredTestDatabase:
        return UNCONFIGURED_TEST_DATABASE_URL
    return env[TEST_DATABASE_URL_ENV]


def main(argv: list[str] | None = None) -> int:
    """Validate a target before migrations run. Prints identity only, never a credential."""
    parser = argparse.ArgumentParser(
        prog="veotrex-database-target",
        description="Validate that a database target is the intended development or test server.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--require-test", action="store_true", help="require an isolated test target"
    )
    group.add_argument(
        "--require-development", action="store_true", help="require the development target"
    )
    arguments = parser.parse_args(argv)
    try:
        if arguments.require_test:
            target = require_test_target(
                os.environ.get(TEST_DATABASE_URL_ENV),
                os.environ.get(DEVELOPMENT_DATABASE_URL_ENV),
            )
        else:
            target = require_development_target(os.environ.get(DEVELOPMENT_DATABASE_URL_ENV))
    except DatabaseTargetError as exc:
        print(f"refusing to continue: {exc}", file=sys.stderr)
        return 2
    print(f"database target accepted: {target.describe()}")
    return 0


if __name__ == "__main__":  # pragma: no cover - console wrapper
    raise SystemExit(main())
