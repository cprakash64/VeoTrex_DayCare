"""The hard environment gate on real face recognition (V1-02B0).

The stage's central safety property: a backend that produces real adult biometric templates,
and the recognition surface that goes with it, cannot come up in staging or production - not
by configuration, not by a mistake in wiring, not by calling the constructor directly.

Three independent refusals are asserted separately here, because the point of having three is
that removing any one of them still leaves the property true:

1. ``Settings`` refuses the backend name outside the permitted environments.
2. ``face_opencv.build`` and ``OpenCvEvalFaceBackend.__init__`` refuse the environment.
3. ``ensure_ready`` refuses an evaluation-only weight outside them.

None of this needs a model file, so it runs in CI on every commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from veotrex_api.config import Settings
from veotrex_api.face_backend import (
    FakeFaceBackend,
    UnavailableFaceBackend,
    supports_recognition,
)
from veotrex_api.face_models import SFACE, YUNET
from veotrex_api.face_opencv import (
    EVALUATION_ENVIRONMENTS,
    FaceBackendUnavailable,
    OpenCvEvalFaceBackend,
    build,
)

DSN = "postgresql+psycopg://veotrex:not-a-real-password@127.0.0.1:1/veotrex"
REFUSED_ENVIRONMENTS = ("staging", "production", "prod", "hostinger", "demo")


def settings(**overrides: object) -> Settings:
    """Settings built from explicit values only.

    Every face field is named here, including the ones a test wants left empty. ``Settings``
    still reads the process environment for fields it is not given, so an exported
    VEOTREX_STAFF_FACE_MODEL_DIR on a developer's machine would otherwise silently satisfy the
    very requirement some of these tests exist to prove is enforced.
    """
    base: dict[str, object] = {
        "environment": "local",
        "app_version": "0.0.0-test",
        "database_url": SecretStr(DSN),
        "staff_media_dir": "/tmp/veotrex-gate-test",  # noqa: S108 - never written to here
        "staff_face_backend": "unavailable",
        "staff_face_model_dir": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


# --------------------------------------------------------------- 1. settings refuse the name
@pytest.mark.parametrize("environment", REFUSED_ENVIRONMENTS)
@pytest.mark.parametrize("backend", ["opencv_eval", "fake"])
def test_settings_refuse_an_evaluation_backend_outside_permitted_environments(
    environment: str, backend: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        settings(
            environment=environment,
            staff_face_backend=backend,
            staff_face_model_dir="/models",
        )
    assert "permitted only in" in str(raised.value)


@pytest.mark.parametrize("environment", sorted(EVALUATION_ENVIRONMENTS))
def test_settings_accept_the_evaluation_backend_in_permitted_environments(
    environment: str,
) -> None:
    resolved = settings(
        environment=environment,
        staff_face_backend="opencv_eval",
        staff_face_model_dir="/models",
    )
    assert resolved.staff_face_backend == "opencv_eval"
    assert resolved.face_evaluation_permitted


def test_production_default_is_unavailable_and_stays_valid() -> None:
    """The production configuration is not merely permitted - it is what you get by default."""
    resolved = settings(environment="production")
    assert resolved.staff_face_backend == "unavailable"
    assert not resolved.face_evaluation_permitted
    assert resolved.staff_face_model_dir == ""


def test_an_unknown_backend_name_is_refused_everywhere() -> None:
    for environment in ("local", "production"):
        with pytest.raises(ValidationError):
            settings(environment=environment, staff_face_backend="insightface")


def test_the_evaluation_backend_requires_a_model_directory() -> None:
    """Without a directory there is nothing to verify a digest against, so the configuration is
    refused at startup rather than becoming a per-request failure."""
    with pytest.raises(ValidationError) as raised:
        settings(environment="local", staff_face_backend="opencv_eval")
    assert "VEOTREX_STAFF_FACE_MODEL_DIR" in str(raised.value)


def test_the_two_definitions_of_a_permitted_environment_agree() -> None:
    """The set lives in settings and in face_opencv. They guard the same property, so one of
    them being widened alone must fail here rather than silently opening the gate."""
    assert Settings.FACE_EVALUATION_ENVIRONMENTS == EVALUATION_ENVIRONMENTS
    assert "staging" not in EVALUATION_ENVIRONMENTS
    assert "production" not in EVALUATION_ENVIRONMENTS


# ------------------------------------------------- 2. the backend refuses the environment
@pytest.mark.parametrize("environment", REFUSED_ENVIRONMENTS)
def test_the_backend_constructor_refuses_a_forbidden_environment(
    environment: str, tmp_path: Path
) -> None:
    """Asserted against the constructor directly, not through settings: code that builds a
    backend without going through settings validation still cannot obtain one."""
    with pytest.raises(FaceBackendUnavailable):
        OpenCvEvalFaceBackend(tmp_path, environment=environment)


def test_build_refuses_a_forbidden_environment_even_with_settings_bypassed(
    tmp_path: Path,
) -> None:
    """Settings are constructed as local and then mutated, which is the shape of the mistake
    this refusal exists for: a value that changed after validation ran."""
    resolved = settings(
        environment="local",
        staff_face_backend="opencv_eval",
        staff_face_model_dir=str(tmp_path),
    )
    object.__setattr__(resolved, "environment", "production")
    with pytest.raises(FaceBackendUnavailable):
        build(resolved)


def test_build_returns_a_backend_in_a_permitted_environment(tmp_path: Path) -> None:
    backend = build(
        settings(
            environment="local",
            staff_face_backend="opencv_eval",
            staff_face_model_dir=str(tmp_path),
        )
    )
    assert isinstance(backend, OpenCvEvalFaceBackend)
    # Constructed but not loaded: no weight has been read and no model is in memory yet.
    assert not backend.ready


# ---------------------------------------------- 3. startup refuses an unusable or foreign model
def test_ensure_ready_refuses_when_the_models_are_absent(tmp_path: Path) -> None:
    backend = OpenCvEvalFaceBackend(tmp_path, environment="local")
    with pytest.raises(FaceBackendUnavailable) as raised:
        backend.ensure_ready()
    assert YUNET.key in str(raised.value)
    assert "model_missing" in str(raised.value)
    assert not backend.ready


def test_ensure_ready_refuses_a_weight_with_the_wrong_digest(tmp_path: Path) -> None:
    """A substituted model of exactly the right size still fails: the digest decides."""
    for artifact in (YUNET, SFACE):
        (tmp_path / artifact.file_name).write_bytes(b"\x00" * artifact.byte_size)
    backend = OpenCvEvalFaceBackend(tmp_path, environment="local")
    with pytest.raises(FaceBackendUnavailable) as raised:
        backend.ensure_ready()
    assert "model_digest_mismatch" in str(raised.value)
    assert not backend.ready


def test_ensure_ready_verifies_the_detector_before_loading_anything(tmp_path: Path) -> None:
    """Both weights are verified before either is handed to OpenCV, so a valid detector paired
    with a tampered recogniser never results in a half-loaded backend."""
    (tmp_path / SFACE.file_name).write_bytes(b"\x00" * SFACE.byte_size)
    backend = OpenCvEvalFaceBackend(tmp_path, environment="local")
    with pytest.raises(FaceBackendUnavailable):
        backend.ensure_ready()
    assert not backend.ready


def test_a_startup_failure_never_names_the_operators_model_directory(tmp_path: Path) -> None:
    private = tmp_path / "operator-private-models"
    private.mkdir()
    backend = OpenCvEvalFaceBackend(private, environment="local")
    with pytest.raises(FaceBackendUnavailable) as raised:
        backend.ensure_ready()
    assert "operator-private-models" not in str(raised.value)


# ------------------------------------------------------------------- recognition capability
def test_the_fail_closed_backend_reports_that_it_cannot_recognise() -> None:
    """The production default has no recognition capability at all, so a permitted environment
    running it still registers no recognition route - environment and capability are two
    separate conditions and both must hold."""
    assert not supports_recognition(UnavailableFaceBackend())
    assert not hasattr(UnavailableFaceBackend(), "extract_query")
    assert settings(environment="local").face_evaluation_permitted
    assert not settings(environment="production").face_evaluation_permitted


def test_the_fake_backend_supports_recognition_so_ci_can_exercise_the_whole_path() -> None:
    assert supports_recognition(FakeFaceBackend())
    assert supports_recognition(OpenCvEvalFaceBackend(Path("/nonexistent"), environment="test"))
