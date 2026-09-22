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


def test_the_project_is_installed_non_editable(dockerfile: str) -> None:
    """An editable install records the BUILDER's source path and cannot resolve at runtime.

    uv writes that path into _editable_impl_veotrex_api.pth. It shipped once: `import veotrex_api`
    failed under uvicorn, while alembic kept working because alembic.ini sets prepend_sys_path,
    which masked the broken install and made the image look half-functional.
    """
    syncs = [i for i in _instructions(dockerfile) if "uv sync" in i]
    assert syncs, "expected the image to install dependencies with uv sync"
    installing = [i for i in syncs if "--no-install-project" not in i]
    assert installing, "expected one uv sync that installs the project itself"
    for instruction in installing:
        assert "--no-editable" in instruction, (
            "the project must be installed non-editable; an editable install bakes the builder "
            f"source path into a .pth that does not exist at runtime: {instruction}"
        )


def test_entry_points_the_deployment_relies_on_are_console_scripts(dockerfile: str) -> None:
    """CMD and the migration command both exec console scripts, which is why the shebang matters."""
    assert '"uvicorn"' in dockerfile
    compose = (REPOSITORY / "infra" / "staging" / "hostinger" / "compose.yaml").read_text()
    assert '"alembic"' in compose


# ----------------------------------------------- no evaluation-only dependency in the image
def test_opencv_is_a_dependency_group_and_never_a_runtime_dependency() -> None:
    """The control-plane image must not contain OpenCV (V1-02B0).

    The evaluation face backend is refused in staging and production, so shipping its 90 MB
    dependency there would add attack surface for a capability that cannot be used. Declaring
    it as a PEP 735 group rather than a project dependency is what keeps it out, because the
    image builds with ``--no-dev`` and uv installs no groups by default.
    """
    manifest = (REPOSITORY / "apps" / "api" / "pyproject.toml").read_text()
    # Comments are stripped: both halves discuss OpenCV, and only the declarations decide.
    declarations = "\n".join(
        line for line in manifest.splitlines() if not line.lstrip().startswith("#")
    )
    runtime, separator, groups = declarations.partition("[dependency-groups]")
    assert separator, "the face-eval dependency group is expected to exist"
    assert "opencv" in groups, "the face-eval group is expected to declare OpenCV"
    assert "opencv" not in runtime, "OpenCV must not be a runtime dependency of veotrex-api"
    assert "numpy" not in runtime, "NumPy must not be a runtime dependency of veotrex-api"


def test_the_image_installs_no_dependency_groups(dockerfile: str) -> None:
    """Both sync steps must keep ``--no-dev``; dropping it would pull the default group in and
    is the single edit that would put an evaluation-only dependency into production."""
    syncs = [line for line in _instructions(dockerfile) if "uv sync" in line]
    assert syncs, "the image is expected to install dependencies with uv sync"
    for statement in syncs:
        assert "--no-dev" in statement
        assert "--all-groups" not in statement
        assert "--group" not in statement


def test_no_face_model_weight_is_copied_into_the_image(dockerfile: str) -> None:
    """Weights are fetched by an operator into a git-ignored directory for local evaluation
    only; nothing may carry one onto the control plane."""
    for statement in _instructions(dockerfile):
        assert ".onnx" not in statement
        assert "artifacts/models" not in statement
