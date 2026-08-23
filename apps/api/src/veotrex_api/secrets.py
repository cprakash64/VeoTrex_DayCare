import os
from typing import Protocol

from pydantic import SecretStr


class SecretResolutionError(Exception):
    pass


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> SecretStr: ...


class EnvironmentSecretResolver:
    """Development adapter. Production should resolve managed secret references."""

    def resolve(self, secret_ref: str) -> SecretStr:
        prefix = "env:"
        if not secret_ref.startswith(prefix):
            raise SecretResolutionError("unsupported secret reference")
        variable = secret_ref.removeprefix(prefix)
        if not variable or variable not in os.environ:
            raise SecretResolutionError("referenced secret is unavailable")
        value = os.environ[variable]
        if not value:
            raise SecretResolutionError("referenced secret is empty")
        return SecretStr(value)
