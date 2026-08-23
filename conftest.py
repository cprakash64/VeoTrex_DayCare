import os

import pytest

os.environ.setdefault(
    "VEOTREX_DATABASE_URL",
    "postgresql+psycopg://veotrex:veotrex_local_only@localhost:5432/veotrex",
)
os.environ.setdefault("VEOTREX_ENVIRONMENT", "test")
os.environ.setdefault("VEOTREX_APP_VERSION", "0.1.0-test")


@pytest.fixture
def settings():
    from veotrex_api.config import Settings

    return Settings(_env_file=None)
