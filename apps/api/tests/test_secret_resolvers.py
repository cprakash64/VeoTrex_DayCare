"""Secret references resolve from the environment or an orchestrator secret mount, or fail closed.

Deployed environments mount secrets as files so the values stay out of the process environment,
where `docker inspect` and /proc/<pid>/environ would expose them. Every value here is synthetic.
"""

from pathlib import Path

import pytest

from veotrex_api.secrets import (
    DefaultSecretResolver,
    EnvironmentSecretResolver,
    FileSecretResolver,
    SecretResolutionError,
)

SYNTHETIC = "synthetic-secret-value-not-real"


def write_secret(tmp_path: Path, content: str, name: str = "secret") -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


# --------------------------------------------------------------------- file references
def test_file_reference_reads_the_value_and_strips_a_trailing_newline(tmp_path: Path) -> None:
    path = write_secret(tmp_path, SYNTHETIC + "\n")
    resolved = FileSecretResolver().resolve(f"file:{path}")
    assert resolved.get_secret_value() == SYNTHETIC
    # A file written without a newline resolves identically.
    bare = write_secret(tmp_path, SYNTHETIC, "bare")
    assert FileSecretResolver().resolve(f"file:{bare}").get_secret_value() == SYNTHETIC


@pytest.mark.parametrize("content", ["", "\n", "   ", "\t\n  \n"])
def test_empty_or_whitespace_only_secret_files_fail_closed(tmp_path: Path, content: str) -> None:
    path = write_secret(tmp_path, content)
    with pytest.raises(SecretResolutionError, match="empty"):
        FileSecretResolver().resolve(f"file:{path}")


def test_missing_file_fails_closed_without_leaking_detail(tmp_path: Path) -> None:
    with pytest.raises(SecretResolutionError) as caught:
        FileSecretResolver().resolve(f"file:{tmp_path / 'absent'}")
    assert "unavailable" in str(caught.value)
    # No errno text, no path contents.
    assert "No such file" not in str(caught.value)


@pytest.mark.parametrize("reference", ["file:", "file:relative/path", "file:./secret"])
def test_non_absolute_file_references_are_refused(reference: str) -> None:
    with pytest.raises(SecretResolutionError, match="absolute path"):
        FileSecretResolver().resolve(reference)


def test_oversized_secret_file_is_refused(tmp_path: Path) -> None:
    path = write_secret(tmp_path, "x" * (FileSecretResolver.MAX_BYTES + 10))
    with pytest.raises(SecretResolutionError, match="permitted size"):
        FileSecretResolver().resolve(f"file:{path}")


def test_non_utf8_secret_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "binary"
    path.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(SecretResolutionError, match="UTF-8"):
        FileSecretResolver().resolve(f"file:{path}")


def test_file_resolver_rejects_other_schemes() -> None:
    with pytest.raises(SecretResolutionError, match="unsupported"):
        FileSecretResolver().resolve("env:SOME_NAME")


# --------------------------------------------------------------------- dispatching resolver
def test_default_resolver_dispatches_on_scheme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_secret(tmp_path, SYNTHETIC)
    monkeypatch.setenv("VEOTREX_SYNTHETIC_SECRET", SYNTHETIC + "-from-env")
    resolver = DefaultSecretResolver()
    assert resolver.resolve(f"file:{path}").get_secret_value() == SYNTHETIC
    assert (
        resolver.resolve("env:VEOTREX_SYNTHETIC_SECRET").get_secret_value()
        == SYNTHETIC + "-from-env"
    )


def test_default_resolver_still_fails_closed_on_unknown_scheme() -> None:
    with pytest.raises(SecretResolutionError, match="unsupported"):
        DefaultSecretResolver().resolve("vault://somewhere")


def test_environment_resolver_behaviour_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEOTREX_ABSENT_SECRET", raising=False)
    with pytest.raises(SecretResolutionError, match="unavailable"):
        EnvironmentSecretResolver().resolve("env:VEOTREX_ABSENT_SECRET")
    monkeypatch.setenv("VEOTREX_EMPTY_SECRET", "")
    with pytest.raises(SecretResolutionError):
        EnvironmentSecretResolver().resolve("env:VEOTREX_EMPTY_SECRET")


def test_resolution_failures_never_contain_the_secret(tmp_path: Path) -> None:
    path = write_secret(tmp_path, "x" * (FileSecretResolver.MAX_BYTES + 10))
    with pytest.raises(SecretResolutionError) as caught:
        FileSecretResolver().resolve(f"file:{path}")
    assert "x" * 32 not in str(caught.value)
