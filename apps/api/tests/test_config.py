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
