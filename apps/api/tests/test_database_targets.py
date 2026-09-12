"""The destructive-test target must be provably isolated from development, or be refused.

No PostgreSQL server is required: every check is structural. All URLs here are synthetic and
carry placeholder credentials only.
"""

import pytest

from veotrex_api.database_targets import (
    UNCONFIGURED_TEST_DATABASE_URL,
    DatabaseTargetError,
    UnconfiguredTestDatabase,
    UnsafeTestDatabase,
    parse_database_url,
    require_development_target,
    require_test_target,
    resolve_test_database_url,
)

DEVELOPMENT = "postgresql+psycopg://veotrex:placeholder@127.0.0.1:5432/veotrex"
ISOLATED_TEST = "postgresql+psycopg://veotrex_test:placeholder@127.0.0.1:55433/veotrex_test"


def test_parsing_keeps_identity_and_discards_the_credential() -> None:
    target = parse_database_url(ISOLATED_TEST)
    assert (target.host, target.port, target.database) == ("127.0.0.1", 55433, "veotrex_test")
    assert target.cluster == ("127.0.0.1", 55433)
    # Nothing rendered for diagnostics may carry the credential.
    assert "placeholder" not in target.describe()
    assert target.describe() == "127.0.0.1:55433/veotrex_test"
    # A URL without an explicit port still identifies a cluster.
    assert parse_database_url("postgresql+psycopg://u@127.0.0.1/veotrex_test").port == 5432


@pytest.mark.parametrize(
    "url",
    [
        "mysql://user@127.0.0.1:3306/veotrex_test",
        "postgresql+psycopg://user@/veotrex_test",
        "postgresql+psycopg://user@127.0.0.1:5432/",
    ],
)
def test_malformed_urls_are_refused(url: str) -> None:
    with pytest.raises(DatabaseTargetError):
        parse_database_url(url)


# ------------------------------------------------------------------ A: nothing configured
def test_missing_test_url_blocks_the_destructive_suite() -> None:
    with pytest.raises(UnconfiguredTestDatabase, match="is not set"):
        require_test_target(None, DEVELOPMENT)
    with pytest.raises(UnconfiguredTestDatabase):
        require_test_target("", DEVELOPMENT)


# ------------------------------------------------------------------ B: development database
@pytest.mark.parametrize("database", ["veotrex", "postgres", "template1"])
def test_development_and_system_databases_are_refused(database: str) -> None:
    with pytest.raises(UnsafeTestDatabase, match="development or system database"):
        require_test_target(f"postgresql+psycopg://u@127.0.0.1:55433/{database}", DEVELOPMENT)


def test_unrecognised_test_database_name_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabase, match="must end in"):
        require_test_target("postgresql+psycopg://u@127.0.0.1:55433/scratch", DEVELOPMENT)


# ------------------------------------------------------------------ C: identical to development
def test_test_url_equal_to_development_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabase):
        require_test_target(DEVELOPMENT, DEVELOPMENT)


# ------------------------------------------------------------------ D: non-loopback host
@pytest.mark.parametrize(
    "host", ["db.production.example", "192.168.0.10", "staging-db.internal", "10.0.0.5"]
)
def test_non_loopback_targets_are_refused(host: str) -> None:
    with pytest.raises(UnsafeTestDatabase, match="loopback"):
        require_test_target(f"postgresql+psycopg://u@{host}:55433/veotrex_test", DEVELOPMENT)


# ------------------------------------------------------------------ E: accepted target
def test_dedicated_isolated_target_is_accepted() -> None:
    target = require_test_target(ISOLATED_TEST, DEVELOPMENT)
    assert target.database == "veotrex_test"
    assert target.cluster != parse_database_url(DEVELOPMENT).cluster
    # localhost is an acceptable spelling of loopback.
    assert (
        require_test_target(
            "postgresql+psycopg://u@localhost:55433/veotrex_test", DEVELOPMENT
        ).database
        == "veotrex_test"
    )


# ------------------------------------------------------------------ F: cluster identity decides
def test_same_database_name_on_the_development_cluster_is_refused() -> None:
    """Roles are cluster-wide: a `_test` database inside the development server is NOT isolated."""
    same_cluster = "postgresql+psycopg://u@127.0.0.1:5432/veotrex_test"
    with pytest.raises(UnsafeTestDatabase, match="cluster"):
        require_test_target(same_cluster, DEVELOPMENT)
    # The identical database name on a different cluster is fine - the port makes it another server.
    assert require_test_target(ISOLATED_TEST, DEVELOPMENT).port == 55433


def test_cluster_check_is_skipped_only_when_no_development_target_is_known() -> None:
    accepted = require_test_target(ISOLATED_TEST, None)
    assert accepted.database == "veotrex_test"


# ------------------------------------------------------------------ development migrations
def test_development_target_refuses_a_test_database_and_requires_configuration() -> None:
    assert require_development_target(DEVELOPMENT).database == "veotrex"
    with pytest.raises(DatabaseTargetError, match="is not set"):
        require_development_target(None)
    with pytest.raises(DatabaseTargetError, match="test database"):
        require_development_target(ISOLATED_TEST)


# ------------------------------------------------------------------ resolution used by conftest
def test_resolution_falls_back_to_an_unreachable_sentinel_never_to_development() -> None:
    resolved = resolve_test_database_url({"VEOTREX_DATABASE_URL": DEVELOPMENT})
    assert resolved == UNCONFIGURED_TEST_DATABASE_URL
    sentinel = parse_database_url(resolved)
    assert sentinel.port == 1  # nothing listens there
    assert sentinel.cluster != parse_database_url(DEVELOPMENT).cluster
    assert "@" not in UNCONFIGURED_TEST_DATABASE_URL.split("//", 1)[1].split("/")[0].replace(
        "veotrex_test@", ""
    )


def test_resolution_raises_on_an_unsafe_configured_target() -> None:
    with pytest.raises(UnsafeTestDatabase):
        resolve_test_database_url(
            {"VEOTREX_TEST_DATABASE_URL": DEVELOPMENT, "VEOTREX_DATABASE_URL": DEVELOPMENT}
        )


def test_resolution_returns_a_configured_isolated_target() -> None:
    assert (
        resolve_test_database_url(
            {"VEOTREX_TEST_DATABASE_URL": ISOLATED_TEST, "VEOTREX_DATABASE_URL": DEVELOPMENT}
        )
        == ISOLATED_TEST
    )


def test_resolution_is_single_application_and_refuses_self_comparison() -> None:
    """Resolution must be applied once, by conftest; a second pass compares the target to itself.

    conftest places the validated test URL into VEOTREX_DATABASE_URL. Calling resolution again in
    that state used to abort collection of the database-backed vault tests, because the guard
    correctly saw one cluster on both sides. The guard stays strict - relaxing "development equals
    test" would let both variables point at the development cluster - so callers consume the
    already-validated value instead of re-resolving.
    """
    environ = {"VEOTREX_TEST_DATABASE_URL": ISOLATED_TEST, "VEOTREX_DATABASE_URL": DEVELOPMENT}
    resolved = resolve_test_database_url(environ)
    assert resolved == ISOLATED_TEST

    # Simulate conftest having mapped the validated target into the application setting.
    environ["VEOTREX_DATABASE_URL"] = resolved
    with pytest.raises(UnsafeTestDatabase, match="cluster"):
        resolve_test_database_url(environ)
