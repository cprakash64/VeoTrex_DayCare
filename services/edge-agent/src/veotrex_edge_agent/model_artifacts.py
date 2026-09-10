from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1
ArtifactType = Literal["onnx", "plan", "checkpoint", "status"]
Precision = Literal["none", "fp32", "fp16"]


class ArtifactValidationError(ValueError):
    """A safe artifact metadata or trust-boundary failure."""


@dataclass(frozen=True, slots=True)
class PlatformCompatibility:
    architecture: str
    l4t: str
    tensorrt: str
    cuda: str


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    schema_version: int
    model_family: str
    model_variant: str
    artifact_type: ArtifactType
    artifact_status: str
    upstream_url: str
    upstream_repository: str
    upstream_revision: str
    retrieved_at: str | None
    sha256: str | None
    byte_size: int | None
    license_identifier: str
    license_evidence_url: str
    inputs: tuple[dict[str, Any], ...]
    outputs: tuple[dict[str, Any], ...]
    onnx_opset: int | None
    compatibility: PlatformCompatibility
    precision: Precision
    engine_build: dict[str, Any] | None
    parent_onnx_sha256: str | None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ArtifactManifest:
        expected = {
            "schema_version",
            "model_family",
            "model_variant",
            "artifact_type",
            "artifact_status",
            "upstream_url",
            "upstream_repository",
            "upstream_revision",
            "retrieved_at",
            "sha256",
            "byte_size",
            "license_identifier",
            "license_evidence_url",
            "inputs",
            "outputs",
            "onnx_opset",
            "compatibility",
            "precision",
            "engine_build",
            "parent_onnx_sha256",
        }
        if set(value) != expected:
            raise ArtifactValidationError("invalid_manifest_fields")
        if value["schema_version"] != SCHEMA_VERSION:
            raise ArtifactValidationError("unsupported_manifest_version")
        if value["artifact_type"] not in {"onnx", "plan", "checkpoint", "status"}:
            raise ArtifactValidationError("invalid_artifact_type")
        if value["precision"] not in {"none", "fp32", "fp16"}:
            raise ArtifactValidationError("invalid_precision")
        if not all(
            isinstance(value[field], str) and value[field].startswith("https://")
            for field in ("upstream_url", "upstream_repository", "license_evidence_url")
        ):
            raise ArtifactValidationError("untrusted_manifest_url")
        digest = value["sha256"]
        if digest is not None and not _is_sha256(digest):
            raise ArtifactValidationError("invalid_sha256")
        parent_digest = value["parent_onnx_sha256"]
        if value["artifact_type"] == "plan" and not _is_sha256(parent_digest):
            raise ArtifactValidationError("plan_missing_parent_onnx")
        compatibility = value["compatibility"]
        if not isinstance(compatibility, dict) or set(compatibility) != {
            "architecture",
            "l4t",
            "tensorrt",
            "cuda",
        }:
            raise ArtifactValidationError("invalid_compatibility")
        if not isinstance(value["inputs"], list) or not isinstance(value["outputs"], list):
            raise ArtifactValidationError("invalid_tensor_contract")
        return cls(
            **{key: item for key, item in value.items() if key != "compatibility"},
            compatibility=PlatformCompatibility(**compatibility),
        )

    @classmethod
    def load(cls, path: Path) -> ArtifactManifest:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("manifest_unreadable") from exc
        if not isinstance(value, dict):
            raise ArtifactValidationError("invalid_manifest")
        return cls.from_dict(value)


def trusted_artifact_path(root: Path, candidate: Path) -> Path:
    trusted_root = root.resolve(strict=True)
    candidate_absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    try:
        relative = candidate_absolute.relative_to(trusted_root)
    except ValueError:
        raise ArtifactValidationError("artifact_outside_trusted_root") from None
    if ".." in relative.parts:
        raise ArtifactValidationError("artifact_traversal_rejected")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file() or not resolved.is_relative_to(trusted_root):
        raise ArtifactValidationError("artifact_outside_trusted_root")
    current = trusted_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactValidationError("artifact_symlink_rejected")
    return resolved


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(root: Path, path: Path, manifest: ArtifactManifest) -> Path:
    resolved = trusted_artifact_path(root, path)
    if manifest.sha256 is None or sha256_file(resolved) != manifest.sha256:
        raise ArtifactValidationError("artifact_hash_mismatch")
    if manifest.byte_size is None or resolved.stat().st_size != manifest.byte_size:
        raise ArtifactValidationError("artifact_size_mismatch")
    return resolved


def validate_compatibility(manifest: ArtifactManifest, expected: PlatformCompatibility) -> None:
    if manifest.compatibility != expected:
        raise ArtifactValidationError("platform_incompatible")
