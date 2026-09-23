"""The audited model registry and its trust boundary (V1-02B0).

Needs no model weight: every test either inspects the recorded audit, or builds a small file
and proves the loader refuses it. That is what lets CI run these on every commit.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from veotrex_api.face_models import (
    REGISTRY,
    SFACE,
    YUNET,
    FaceModelArtifact,
    FaceModelError,
    provenance,
    resolve,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FETCH_SCRIPT = REPOSITORY_ROOT / "infra" / "local" / "fetch-face-eval-models.sh"


def plant(directory: Path, artifact: FaceModelArtifact, data: bytes) -> Path:
    path = directory / artifact.file_name
    path.write_bytes(data)
    return path


def exact(artifact: FaceModelArtifact) -> bytes:
    """Bytes that are the right length but the wrong content: enough to reach the digest
    check, which is the check under test."""
    return b"\x00" * artifact.byte_size


# ----------------------------------------------------------------------------- the audit itself
def test_every_registered_artifact_records_a_complete_audit() -> None:
    assert set(REGISTRY) == {YUNET.file_name, SFACE.file_name}
    for artifact in REGISTRY.values():
        assert len(artifact.sha256) == 64
        assert set(artifact.sha256) <= set("0123456789abcdef")
        assert artifact.byte_size > 0
        assert artifact.approval in {
            "PRODUCTION_APPROVED",
            "LOCAL_EVALUATION_ONLY",
            "REJECTED",
        }
        for url in (
            artifact.upstream_repository,
            artifact.upstream_url,
            artifact.license_evidence_url,
            artifact.approval_evidence_url,
        ):
            assert url.startswith("https://")
        assert artifact.upstream_revision
        assert artifact.training_data_provenance
        assert artifact.commercial_use


def test_the_registry_is_keyed_by_the_file_name_it_will_look_for() -> None:
    """The loader never takes a file name from configuration or a request, so the key and the
    artifact's own name must not be able to drift apart."""
    for file_name, artifact in REGISTRY.items():
        assert file_name == artifact.file_name


def test_yunet_is_production_approved_and_sface_is_not() -> None:
    """The audit's conclusion, asserted rather than only documented: MIT covers YuNet's weights
    outright, while SFace's commercial and training-data position is unresolved upstream."""
    assert YUNET.license_identifier == "MIT"
    assert YUNET.approval == "PRODUCTION_APPROVED"
    assert YUNET.production_approved

    assert SFACE.license_identifier == "Apache-2.0"
    assert SFACE.approval == "LOCAL_EVALUATION_ONLY"
    assert not SFACE.production_approved
    assert "UNRESOLVED" in SFACE.training_data_provenance
    assert "313" in SFACE.approval_evidence_url


def test_no_insightface_weight_is_registered() -> None:
    """InsightFace's pretrained models are non-commercial-research only and no commercial
    licence has been obtained, so none may appear in the registry under any name."""
    for artifact in REGISTRY.values():
        haystack = f"{artifact.upstream_repository} {artifact.upstream_url} {artifact.model_id}"
        assert "insightface" not in haystack.lower()
        assert "deepinsight" not in haystack.lower()


def test_provenance_is_a_complete_record_of_one_artifact() -> None:
    record = provenance(SFACE)
    assert record["sha256"] == SFACE.sha256
    assert record["approval"] == "LOCAL_EVALUATION_ONLY"
    assert len(record) == 16


# ------------------------------------------------------------------ the fetch script agrees
def test_the_operator_fetch_script_pins_the_same_digests_as_the_registry() -> None:
    """The script is what actually puts bytes on disk. If its digests could drift from the
    registry's, the operator could fetch one file and the application verify against another."""
    script = FETCH_SCRIPT.read_text(encoding="utf-8")
    assert f'REVISION="{YUNET.upstream_revision}"' in script
    for artifact in REGISTRY.values():
        assert f"|{artifact.file_name}|{artifact.sha256}|{artifact.byte_size}" in script


def test_the_fetch_script_is_the_only_thing_that_downloads_a_model() -> None:
    """No application module may fetch a weight: not at import, not at startup, not per
    request. The audited evidence URLs are data in the registry, never something fetched."""
    source_root = REPOSITORY_ROOT / "apps" / "api" / "src" / "veotrex_api"
    for module in sorted(source_root.glob("face*.py")):
        text = module.read_text(encoding="utf-8")
        for forbidden in ("urlopen", "urlretrieve", "httpx.get", "requests.get", "curl "):
            assert forbidden not in text, f"{module.name} appears to fetch a model"


# --------------------------------------------------------------------------------- resolution
def test_resolve_accepts_a_file_whose_size_and_digest_match(tmp_path: Path) -> None:
    artifact = FaceModelArtifact(
        key="probe",
        role="detection",
        file_name="probe.onnx",
        model_id="probe",
        model_version="1",
        upstream_repository="https://example.invalid/repo",
        upstream_revision="v1",
        upstream_url="https://example.invalid/repo/probe.onnx",
        license_identifier="MIT",
        license_evidence_url="https://example.invalid/repo/LICENSE",
        training_data_provenance="synthetic",
        commercial_use="permitted",
        approval="PRODUCTION_APPROVED",
        approval_evidence_url="https://example.invalid/repo/LICENSE",
        sha256=hashlib.sha256(b"weights").hexdigest(),
        byte_size=len(b"weights"),
    )
    plant(tmp_path, artifact, b"weights")
    assert resolve(tmp_path, artifact) == (tmp_path / "probe.onnx").resolve()


def test_resolve_refuses_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FaceModelError) as raised:
        resolve(tmp_path / "absent", YUNET)
    assert raised.value.category == "model_dir_unavailable"


def test_resolve_refuses_a_directory_that_is_a_file(tmp_path: Path) -> None:
    ordinary = tmp_path / "not-a-directory"
    ordinary.write_bytes(b"")
    with pytest.raises(FaceModelError) as raised:
        resolve(ordinary, YUNET)
    assert raised.value.category == "model_dir_unavailable"


def test_resolve_refuses_a_missing_model(tmp_path: Path) -> None:
    with pytest.raises(FaceModelError) as raised:
        resolve(tmp_path, YUNET)
    assert raised.value.category == "model_missing"


def test_resolve_refuses_a_model_of_the_wrong_size(tmp_path: Path) -> None:
    plant(tmp_path, YUNET, b"too short")
    with pytest.raises(FaceModelError) as raised:
        resolve(tmp_path, YUNET)
    assert raised.value.category == "model_size_mismatch"


def test_resolve_refuses_a_model_of_the_right_size_and_the_wrong_content(tmp_path: Path) -> None:
    """The size check alone would pass here. Substituting a different model of the same size is
    exactly what the digest exists to stop."""
    plant(tmp_path, YUNET, exact(YUNET))
    with pytest.raises(FaceModelError) as raised:
        resolve(tmp_path, YUNET)
    assert raised.value.category == "model_digest_mismatch"


def test_resolve_refuses_a_symlink_even_when_it_points_at_a_valid_file(tmp_path: Path) -> None:
    """A link inside the model directory must not be able to aim the loader somewhere else,
    including somewhere whose content would pass every other check."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = elsewhere / "real.onnx"
    real.write_bytes(exact(YUNET))
    models = tmp_path / "models"
    models.mkdir()
    os.symlink(real, models / YUNET.file_name)
    with pytest.raises(FaceModelError) as raised:
        resolve(models, YUNET)
    assert raised.value.category == "model_symlink_rejected"


def test_resolve_refuses_a_directory_where_a_model_should_be(tmp_path: Path) -> None:
    (tmp_path / YUNET.file_name).mkdir()
    with pytest.raises(FaceModelError) as raised:
        resolve(tmp_path, YUNET)
    assert raised.value.category in {"model_size_mismatch", "model_not_a_regular_file"}


def test_a_model_error_never_reveals_where_it_looked(tmp_path: Path) -> None:
    secret = tmp_path / "some-private-operator-path"
    secret.mkdir()
    with pytest.raises(FaceModelError) as raised:
        resolve(secret, YUNET)
    assert "some-private-operator-path" not in str(raised.value)
    assert raised.value.category == "model_missing"
