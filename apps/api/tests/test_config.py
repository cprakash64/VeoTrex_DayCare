import pytest
from pydantic import ValidationError

from veotrex_api.config import Settings


def test_configuration_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEOTREX_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", app_version="test")


def test_database_url_is_secret() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+psycopg://user:secret@db/database",
        app_version="test",
    )
    assert "secret" not in repr(settings)
    assert settings.database_url.get_secret_value().endswith("@db/database")


def test_identity_settings_reject_inexact_issuer_and_symmetric_algorithm() -> None:
    with pytest.raises(ValidationError, match="OIDC issuer"):
        Settings(
            _env_file=None,
            environment="test",
            database_url="postgresql+psycopg://unused",
            app_version="test",
            oidc_issuer="http://unsafe.example",
        )
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+psycopg://unused",
        app_version="test",
        oidc_allowed_algorithms="HS256",
    )
    with pytest.raises(ValueError, match="asymmetric allowlist"):
        _ = settings.oidc_algorithms


def test_migration_database_url_is_separate_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alembic must be able to run as a distinct migration identity while the API keeps its
    own restricted DSN; when no migration DSN is configured the single DSN is used."""
    monkeypatch.delenv("VEOTREX_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("VEOTREX_MIGRATION_DATABASE_URL_REF", raising=False)
    single = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+psycopg://veotrex_api:placeholder@db/veotrex",
        app_version="test",
    )
    assert single.migration_database_url is None
    assert single.effective_migration_database_url is single.database_url

    split = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+psycopg://veotrex_api:placeholder@db/veotrex",
        migration_database_url="postgresql+psycopg://veotrex:secret@db/veotrex",
        app_version="test",
    )
    assert split.effective_migration_database_url.get_secret_value().startswith(
        "postgresql+psycopg://veotrex:"
    )
    assert split.database_url.get_secret_value().startswith("postgresql+psycopg://veotrex_api:")
    assert "placeholder" not in repr(split) and "secret" not in repr(split)

    monkeypatch.setenv("MIGRATION_DSN_FOR_TEST", "postgresql+psycopg://veotrex:pw@db/veotrex")
    by_reference = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+psycopg://veotrex_api:placeholder@db/veotrex",
        migration_database_url_ref="env:MIGRATION_DSN_FOR_TEST",
        app_version="test",
    )
    assert by_reference.effective_migration_database_url.get_secret_value().endswith(
        "pw@db/veotrex"
    )
    with pytest.raises(ValidationError, match="never both"):
        Settings(
            _env_file=None,
            environment="test",
            database_url="postgresql+psycopg://veotrex_api:placeholder@db/veotrex",
            migration_database_url="postgresql+psycopg://veotrex:secret@db/veotrex",
            migration_database_url_ref="env:MIGRATION_DSN_FOR_TEST",
            app_version="test",
        )
