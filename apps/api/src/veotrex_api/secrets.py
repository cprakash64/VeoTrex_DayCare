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


class FileSecretResolver:
    """Resolves ``file:<absolute path>`` references, for container/orchestrator secret mounts.

    Docker secrets, Kubernetes secret volumes and root-owned host secret directories all present
    a secret as a file. Reading the file keeps the value out of the process environment, where it
    would otherwise be visible through ``docker inspect`` and ``/proc/<pid>/environ``.

    A trailing newline is stripped because secret files are routinely written by shell
    redirection. Nothing here echoes the value, and errors never include file contents.
    """

    PREFIX = "file:"
    MAX_BYTES = 8192

    def resolve(self, secret_ref: str) -> SecretStr:
        if not secret_ref.startswith(self.PREFIX):
            raise SecretResolutionError("unsupported secret reference")
        path = secret_ref.removeprefix(self.PREFIX)
        if not path or not os.path.isabs(path):
            raise SecretResolutionError("secret file reference must be an absolute path")
        try:
            with open(path, "rb") as handle:
                raw = handle.read(self.MAX_BYTES + 1)
        except OSError:
            # Never surface errno detail or the path's contents.
            raise SecretResolutionError("referenced secret is unavailable") from None
        if len(raw) > self.MAX_BYTES:
            raise SecretResolutionError("referenced secret exceeds the permitted size")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise SecretResolutionError("referenced secret is not valid UTF-8") from None
        if not value:
            raise SecretResolutionError("referenced secret is empty")
        return SecretStr(value)


class DefaultSecretResolver:
    """Dispatches on the reference scheme so one deployment can mix both mechanisms.

    ``env:NAME``  - process environment, the existing development path.
    ``file:/abs`` - orchestrator secret mount, preferred for deployed environments.
    """

    def __init__(self) -> None:
        self._environment = EnvironmentSecretResolver()
        self._file = FileSecretResolver()

    def resolve(self, secret_ref: str) -> SecretStr:
        if secret_ref.startswith(FileSecretResolver.PREFIX):
            return self._file.resolve(secret_ref)
        return self._environment.resolve(secret_ref)
