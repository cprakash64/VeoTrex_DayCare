from __future__ import annotations

import json
from pathlib import Path

import pytest

from veotrex_edge_agent.model_artifacts import (
    ArtifactManifest,
    ArtifactValidationError,
    PlatformCompatibility,
    sha256_file,
    trusted_artifact_path,
    validate_compatibility,
    verify_artifact,
)


def manifest_dict(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "model_family": "YOLOX",
        "model_variant": "S",
        "artifact_type": "onnx",
        "artifact_status": "qualified",
        "upstream_url": "https://github.com/example/release/model.onnx",
        "upstream_repository": "https://github.com/example/project",
        "upstream_revision": "v1",
        "retrieved_at": "2026-09-10T00:00:00Z",
        "sha256": "0" * 64,
        "byte_size": 4,
        "license_identifier": "Apache-2.0",
        "license_evidence_url": "https://github.com/example/project/LICENSE",
        "inputs": [{"name": "images", "shape": [1, 3, 640, 640], "dtype": "float32"}],
        "outputs": [{"name": "output", "shape": [1, 8400, 85], "dtype": "float32"}],
        "onnx_opset": 11,
        "compatibility": {
            "architecture": "aarch64",
            "l4t": "39.2.0",
            "tensorrt": "10.16.2.10",
            "cuda": "13.2",
        },
        "precision": "none",
        "engine_build": None,
        "parent_onnx_sha256": None,
    }
    value.update(changes)
    return value


def test_manifest_loads_exact_schema(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_dict()))
    assert ArtifactManifest.load(path).model_variant == "S"


@pytest.mark.parametrize("field", ["unexpected", "schema_version"])
def test_manifest_rejects_unknown_or_versioned_fields(field: str) -> None:
    value = manifest_dict()
    value[field] = 2
    with pytest.raises(ArtifactValidationError):
        ArtifactManifest.from_dict(value)


def test_plan_requires_parent_onnx_hash() -> None:
    with pytest.raises(ArtifactValidationError, match="plan_missing_parent_onnx"):
        ArtifactManifest.from_dict(manifest_dict(artifact_type="plan", precision="fp16"))

    with pytest.raises(ArtifactValidationError, match="plan_missing_parent_onnx"):
        ArtifactManifest.from_dict(
            manifest_dict(artifact_type="plan", precision="fp16", parent_onnx_sha256="bad")
        )


def test_precision_metadata_is_bounded() -> None:
    with pytest.raises(ArtifactValidationError, match="invalid_precision"):
        ArtifactManifest.from_dict(manifest_dict(precision="int8"))


def test_trusted_path_rejects_escape_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "trusted"
    root.mkdir()
    outside = tmp_path / "outside.plan"
    outside.write_bytes(b"plan")
    with pytest.raises(ArtifactValidationError, match="outside_trusted_root"):
        trusted_artifact_path(root, outside)
    link = root / "link.plan"
    link.symlink_to(outside)
    with pytest.raises(ArtifactValidationError):
        trusted_artifact_path(root, link)
    traversal = root / "nested" / ".." / "artifact.plan"
    (root / "artifact.plan").write_bytes(b"plan")
    with pytest.raises(ArtifactValidationError, match="artifact_traversal_rejected"):
        trusted_artifact_path(root, traversal)


def test_hash_and_size_verification(tmp_path: Path) -> None:
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"onnx")
    digest = sha256_file(artifact)
    manifest = ArtifactManifest.from_dict(manifest_dict(sha256=digest))
    assert verify_artifact(tmp_path, artifact, manifest) == artifact
    with pytest.raises(ArtifactValidationError, match="artifact_hash_mismatch"):
        verify_artifact(tmp_path, artifact, ArtifactManifest.from_dict(manifest_dict()))


def test_platform_compatibility_is_exact() -> None:
    manifest = ArtifactManifest.from_dict(manifest_dict())
    expected = PlatformCompatibility("aarch64", "39.2.0", "10.16.2.10", "13.2")
    validate_compatibility(manifest, expected)
    with pytest.raises(ArtifactValidationError, match="platform_incompatible"):
        validate_compatibility(
            manifest, PlatformCompatibility("aarch64", "39.2.0", "11.0.0", "13.2")
        )
