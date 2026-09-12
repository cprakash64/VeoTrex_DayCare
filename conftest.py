"""Test bootstrap.

No database credential appears here. Destructive database tests are pointed at an explicitly
configured, isolated test cluster through ``VEOTREX_TEST_DATABASE_URL``; they never inherit
``VEOTREX_DATABASE_URL``, so they cannot reach development data even by accident.

When no test cluster is configured the suite receives an unreachable sentinel target and the
database-backed tests simply fail to connect. When one *is* configured but is not demonstrably
isolated - a development database name, a non-loopback host, or the same PostgreSQL cluster as
development, whose roles are shared - resolution raises instead of running anything destructive.
"""

import os

import pytest

from veotrex_api.database_targets import resolve_test_database_url

# Resolved before any test imports application settings. Raises on an unsafe configured target.
os.environ["VEOTREX_DATABASE_URL"] = resolve_test_database_url()
os.environ.setdefault("VEOTREX_ENVIRONMENT", "test")
os.environ.setdefault("VEOTREX_APP_VERSION", "0.1.0-test")


@pytest.fixture
def settings():
    from veotrex_api.config import Settings

    return Settings(_env_file=None)
