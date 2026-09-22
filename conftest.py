"""Test bootstrap.

No database credential appears here. Destructive database tests are pointed at an explicitly
configured, isolated test cluster through ``VEOTREX_TEST_DATABASE_URL``; they never inherit
``VEOTREX_DATABASE_URL``, so they cannot reach development data even by accident.

When no test cluster is configured the suite receives an unreachable sentinel target and the
database-backed tests simply fail to connect. When one *is* configured but is not demonstrably
isolated - a development database name, a non-loopback host, or the same PostgreSQL cluster as
development, whose roles are shared - resolution raises instead of running anything destructive.

Two identities, exactly as in production (V1-00A):

* ``VEOTREX_TEST_DATABASE_URL`` is the cluster **admin** identity. It is used only to provision
  the restricted runtime role, to seed and inspect fixtures, and to tear them down. The
  ``admin_settings`` fixture exposes it.
* ``VEOTREX_DATABASE_URL`` (the ``settings`` fixture, and therefore every ``create_app`` and
  service under test) is a ``NOSUPERUSER NOBYPASSRLS`` **runtime** role provisioned at session
  start with a password generated here and held only in memory. Application behaviour and Row
  Level Security are therefore exercised across the same boundary the deployed API has.
"""

import os
import sys

import pytest

from veotrex_api.database_targets import (
    UNCONFIGURED_TEST_DATABASE_URL,
    resolve_test_database_url,
)

TEST_RUNTIME_ROLE = "veotrex_api_test"

# Resolved before any test imports application settings. Raises on an unsafe configured target.
_ADMIN_DATABASE_URL = resolve_test_database_url()
_RUNTIME_DATABASE_URL = UNCONFIGURED_TEST_DATABASE_URL

if _ADMIN_DATABASE_URL != UNCONFIGURED_TEST_DATABASE_URL:
    from veotrex_api.runtime_role import RuntimeRoleError, provision_for_tests

    try:
        provisioned = provision_for_tests(_ADMIN_DATABASE_URL, role=TEST_RUNTIME_ROLE)
    except RuntimeRoleError as exc:
        # A reachable but unmigrated cluster is a setup mistake worth failing loudly, before
        # a hundred tests fail with misleading permission errors.
        print(f"conftest: cannot provision the test runtime role: {exc}", file=sys.stderr)
        raise
    if provisioned is not None:
        _RUNTIME_DATABASE_URL = provisioned

os.environ["VEOTREX_TEST_DATABASE_URL"] = _ADMIN_DATABASE_URL
os.environ["VEOTREX_DATABASE_URL"] = _RUNTIME_DATABASE_URL
os.environ.pop("VEOTREX_DATABASE_URL_REF", None)
os.environ.pop("VEOTREX_MIGRATION_DATABASE_URL", None)
os.environ.pop("VEOTREX_MIGRATION_DATABASE_URL_REF", None)
os.environ.setdefault("VEOTREX_ENVIRONMENT", "test")
# Staff enrollment (V1-02A): the API refuses to start without a writable private media
# directory, so every test app gets a fresh per-session one unless the environment names one.
import tempfile  # noqa: E402

os.environ.setdefault(
    "VEOTREX_STAFF_MEDIA_DIR", tempfile.mkdtemp(prefix="veotrex-staff-media-test-")
)
os.environ.setdefault("VEOTREX_APP_VERSION", "0.1.0-test")


@pytest.fixture
def settings():
    """Runtime identity: what the API process itself connects as."""
    from veotrex_api.config import Settings

    return Settings(_env_file=None)


@pytest.fixture
def admin_settings():
    """Cluster admin identity: fixtures, provisioning and teardown only - never the code under
    test."""
    from veotrex_api.config import Settings

    return Settings(_env_file=None, database_url=_ADMIN_DATABASE_URL)


@pytest.fixture
def runtime_role_name() -> str:
    return TEST_RUNTIME_ROLE
