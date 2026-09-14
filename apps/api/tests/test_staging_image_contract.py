"""The staging control-plane image must be able to execute what it installs. No Docker needed.

uv writes an ABSOLUTE shebang into every console script it installs, so a virtualenv built at one
path and copied to another leaves `alembic`, `uvicorn` and the project's own entry points pointing
at an interpreter the runtime stage does not contain. The scripts are present and exec fails with
ENOENT on the interpreter, which reads as "no such file or directory" for the script itself. That
shipped once: the API crash-looped on `exec /app/.venv/bin/uvicorn` and the migration could not
run, on an image whose build and unit tests were both green.

These are static assertions on the Dockerfile. They pin the path invariant rather than the symptom;
building the image and running a console script inside it would be stronger coverage and belongs in
a CI image-smoke job.
"""

import re
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
API_DOCKERFILE = REPOSITORY / "infra" / "staging" / "Dockerfile.api"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return API_DOCKERFILE.read_text()


def _instructions(text: str) -> list[str]:
    """Logical instructions: continuation lines joined, comments and blanks dropped."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    return [
        line.strip() for line in joined.splitlines() if line.strip() and not line.startswith("#")
    ]


def _runtime_environment_path(text: str) -> str:
    match = re.search(r"UV_PROJECT_ENVIRONMENT=(\S+)", text)
    assert match, "the builder must pin UV_PROJECT_ENVIRONMENT so the venv path is explicit"
    return match.group(1)


def test_virtualenv_is_built_at_the_path_it_is_executed_from(dockerfile: str) -> None:
    """The defect: built at /build/.venv, copied to /app/.venv, every shebang then dangling."""
    environment = _runtime_environment_path(dockerfile)
    copies = [i for i in _instructions(dockerfile) if i.startswith("COPY") and ".venv" in i]
    assert copies, "expected the virtualenv to be copied into the runtime stage"
    for instruction in copies:
        source, destination = instruction.split()[-2:]
        assert source == destination, (
            f"virtualenv copied across paths ({source} -> {destination}); "
            "console-script shebangs are absolute and would not resolve at runtime"
        )
        assert source == environment, f"{source} is not the pinned UV_PROJECT_ENVIRONMENT"


def test_runtime_path_points_at_that_same_virtualenv(dockerfile: str) -> None:
    environment = _runtime_environment_path(dockerfile)
    match = re.search(r'ENV PATH="([^"]+)"', dockerfile)
    assert match, "the runtime stage must put the virtualenv on PATH"
    assert match.group(1).split(":")[0] == f"{environment}/bin"


def test_no_builder_only_path_survives_into_the_runtime_stage(dockerfile: str) -> None:
    """A /build path cannot resolve in the runtime stage: that directory is builder-only."""
    _, _, runtime = dockerfile.partition("AS runtime")
    assert runtime, "expected a named runtime stage"
    for instruction in _instructions(runtime):
        if instruction.startswith("COPY"):
            assert not instruction.split()[-1].startswith("/build"), instruction
        if instruction.startswith("ENV PATH"):
            assert "/build" not in instruction, instruction


def test_entry_points_the_deployment_relies_on_are_console_scripts(dockerfile: str) -> None:
    """CMD and the migration command both exec console scripts, which is why the shebang matters."""
    assert '"uvicorn"' in dockerfile
    compose = (REPOSITORY / "infra" / "staging" / "hostinger" / "compose.yaml").read_text()
    assert '"alembic"' in compose
