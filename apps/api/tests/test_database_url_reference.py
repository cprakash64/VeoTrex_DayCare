"""The database URL may be supplied directly or by secret reference, never ambiguously.

A deployed control plane mounts the DSN as a file so it stays out of the process environment,
where `docker inspect` and /proc/<pid>/environ would expose it. Development and tests keep
supplying the URL directly. Configuring both is refused rather than silently preferring one.
All values here are synthetic.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from veotrex_api.config import Settings
from veotrex_api.secrets import FileSecretResolver

# The repository-wide placeholder convention (README.md, .env.example). Spelled out rather
# than interpolated so the committed literal is what the credential scanner sees and admits.
SYNTHETIC_PLACEHOLDER = "REPLACE_WITH_SYNTHETIC_PASSWORD"
SYNTHETIC_DSN = "postgresql+psycopg://veotrex:REPLACE_WITH_SYNTHETIC_PASSWORD@postgres:5432/veotrex"
BASE = {"_env_file": None, "environment": "test", "app_version": "0.0.0-test"}


@pytest.fixture(autouse=True)
def clear_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest seeds VEOTREX_DATABASE_URL for the suite; these cases control it explicitly.
    monkeypatch.delenv("VEOTREX_DATABASE_URL", raising=False)
    monkeypatch.delenv("VEOTREX_DATABASE_URL_REF", raising=False)


def settings(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------- placeholder wiring
def test_the_placeholder_is_the_credential_in_the_synthetic_dsn() -> None:
    """Without this the "secret never rendered" assertions below could pass vacuously."""
    assert SYNTHETIC_DSN.split("://", 1)[1].split("@", 1)[0] == f"veotrex:{SYNTHETIC_PLACEHOLDER}"


# --------------------------------------------------------------------- direct value
def test_direct_url_still_works_unchanged() -> None:
    resolved = settings(database_url=SYNTHETIC_DSN)
    assert resolved.database_url.get_secret_value() == SYNTHETIC_DSN
    assert SYNTHETIC_PLACEHOLDER not in repr(resolved)


def test_neither_source_configured_still_fails() -> None:
    """The pre-existing guarantee: a missing database URL is a validation error."""
    with pytest.raises(ValidationError):
        settings()


# --------------------------------------------------------------------- reference value
def test_file_reference_supplies_the_url(tmp_path: Path) -> None:
    path = tmp_path / "database_url"
    path.write_text(SYNTHETIC_DSN + "\n")  # trailing newline, as shell redirection writes
    resolved = settings(database_url_ref=f"file:{path}")
    assert resolved.database_url.get_secret_value() == SYNTHETIC_DSN
    # The reference itself is not secret, but the resolved value must never be rendered.
    assert SYNTHETIC_PLACEHOLDER not in repr(resolved)


def test_reference_is_usable_by_every_consumer(tmp_path: Path) -> None:
    """db.py, alembic/env.py and readiness all read settings.database_url unchanged."""
    path = tmp_path / "database_url"
    path.write_text(SYNTHETIC_DSN)
    resolved = settings(database_url_ref=f"file:{path}")
    assert resolved.database_url.get_secret_value().startswith("postgresql+psycopg://")


# --------------------------------------------------------------------- ambiguity fails closed
def test_configuring_both_sources_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "database_url"
    path.write_text(SYNTHETIC_DSN)
    with pytest.raises(ValidationError, match="never both"):
        settings(database_url=SYNTHETIC_DSN, database_url_ref=f"file:{path}")


# --------------------------------------------------------------------- unusable references
def test_missing_referenced_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unusable"):
        settings(database_url_ref=f"file:{tmp_path / 'absent'}")


@pytest.mark.parametrize("reference", ["file:relative/path", "file:./database_url", "file:"])
def test_relative_or_empty_paths_are_refused(reference: str) -> None:
    with pytest.raises(ValidationError):
        settings(database_url_ref=reference)


@pytest.mark.parametrize("content", ["", "\n", "   "])
def test_empty_secret_is_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "database_url"
    path.write_text(content)
    with pytest.raises(ValidationError, match="unusable"):
        settings(database_url_ref=f"file:{path}")


def test_oversized_secret_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "database_url"
    path.write_text("x" * (FileSecretResolver.MAX_BYTES + 10))
    with pytest.raises(ValidationError, match="unusable"):
        settings(database_url_ref=f"file:{path}")


def test_non_utf8_secret_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "database_url"
    path.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ValidationError, match="unusable"):
        settings(database_url_ref=f"file:{path}")


def test_unsupported_scheme_is_refused() -> None:
    with pytest.raises(ValidationError, match="unusable"):
        settings(database_url_ref="vault://somewhere/database_url")


# --------------------------------------------------------------------- no leakage
def test_failure_messages_never_contain_the_secret(tmp_path: Path) -> None:
    path = tmp_path / "database_url"
    path.write_text(SYNTHETIC_DSN + "x" * FileSecretResolver.MAX_BYTES)
    with pytest.raises(ValidationError) as caught:
        settings(database_url_ref=f"file:{path}")
    rendered = str(caught.value)
    assert SYNTHETIC_PLACEHOLDER not in rendered
    assert "postgresql+psycopg" not in rendered
