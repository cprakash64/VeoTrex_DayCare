"""Audited face-model artifacts and their trust boundary (V1-02B0).

The registry below is the authority on which model weights VeoTrex will load, and it lives in
code rather than in a data file on purpose: SHA-256, upstream source and approval status are
security decisions, so a file under the operator's model directory must never be able to
authorise a different weight than the one that was audited.

Every entry records what ADR 0020 reviewed - the upstream repository, the exact revision the
file was read at, its declared licence and the licence file that declares it, the digest and
byte size published by that revision's git-lfs pointer, and the approval the audit reached:

``PRODUCTION_APPROVED``
    Licence and weight provenance are both clear enough for a commercial deployment.
``LOCAL_EVALUATION_ONLY``
    Usable for internal qualification with consenting adults, not for staging or production.
``REJECTED``
    Must not be loaded at all.

Nothing here downloads anything, at startup or on request. The operator fetches the weights
once with ``infra/local/fetch-face-eval-models.sh``, which verifies the same digests; the
application only ever *verifies* a file that is already on disk. ``resolve`` is the single
entry point and fails closed: wrong directory, missing file, symlink, non-regular file, wrong
size or wrong digest all raise, and no model is loaded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Approval = Literal["PRODUCTION_APPROVED", "LOCAL_EVALUATION_ONLY", "REJECTED"]

# A weight is read in bounded chunks; the largest audited artifact is ~37 MiB.
_CHUNK_BYTES = 1024 * 1024
MAX_MODEL_BYTES = 256 * 1024 * 1024


class FaceModelError(Exception):
    """A bounded category. Never a filesystem path, never model internals."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class FaceModelArtifact:
    """One audited model weight. Every field is evidence recorded by the ADR 0020 audit."""

    key: str
    role: Literal["detection", "recognition"]
    file_name: str
    model_id: str
    model_version: str
    upstream_repository: str
    upstream_revision: str
    upstream_url: str
    license_identifier: str
    license_evidence_url: str
    training_data_provenance: str
    commercial_use: str
    approval: Approval
    approval_evidence_url: str
    sha256: str
    byte_size: int

    @property
    def production_approved(self) -> bool:
        return self.approval == "PRODUCTION_APPROVED"


_OPENCV_ZOO = "https://github.com/opencv/opencv_zoo"
# The opencv_zoo release tag the audit read every field at. Pinned, never "main": the weights
# are git-lfs objects and a moving branch could publish a different object under the same name.
_OPENCV_ZOO_REVISION = "4.10.0"


def _zoo_url(directory: str, file_name: str) -> str:
    return f"{_OPENCV_ZOO}/blob/{_OPENCV_ZOO_REVISION}/models/{directory}/{file_name}"


YUNET = FaceModelArtifact(
    key="yunet_2023mar",
    role="detection",
    file_name="face_detection_yunet_2023mar.onnx",
    model_id="yunet",
    model_version="2023mar",
    upstream_repository=_OPENCV_ZOO,
    upstream_revision=_OPENCV_ZOO_REVISION,
    upstream_url=_zoo_url("face_detection_yunet", "face_detection_yunet_2023mar.onnx"),
    license_identifier="MIT",
    license_evidence_url=_zoo_url("face_detection_yunet", "LICENSE"),
    training_data_provenance=(
        "WIDER FACE, stated by the upstream libfacedetection project of the same author "
        "(Shiqi Yu); the zoo directory itself does not restate it."
    ),
    commercial_use=(
        "Permitted. MIT covers every file in the directory, weights included, with no "
        "field-of-use restriction and no separate model terms."
    ),
    approval="PRODUCTION_APPROVED",
    approval_evidence_url=_zoo_url("face_detection_yunet", "LICENSE"),
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    byte_size=232589,
)

SFACE = FaceModelArtifact(
    key="sface_2021dec",
    role="recognition",
    file_name="face_recognition_sface_2021dec.onnx",
    model_id="sface",
    model_version="2021dec",
    upstream_repository=_OPENCV_ZOO,
    upstream_revision=_OPENCV_ZOO_REVISION,
    upstream_url=_zoo_url("face_recognition_sface", "face_recognition_sface_2021dec.onnx"),
    license_identifier="Apache-2.0",
    license_evidence_url=_zoo_url("face_recognition_sface", "LICENSE"),
    training_data_provenance=(
        "UNRESOLVED. The directory README says the file encodes a MobileFaceNet trained with "
        "the SFace loss, converted from zhongyy/SFace, but neither the README nor the pull "
        "request that added the December 2021 weight names the dataset it was trained on."
    ),
    commercial_use=(
        "AMBIGUOUS. Apache-2.0 is declared for the directory, but opencv_zoo issue 313 "
        "(opened 2026-07-22, still open with no maintainer answer) asks upstream to confirm "
        "that it covers commercial inference with these weights and to state their "
        "training-data provenance. Treated as unresolved until upstream answers."
    ),
    approval="LOCAL_EVALUATION_ONLY",
    approval_evidence_url=f"{_OPENCV_ZOO}/issues/313",
    sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    byte_size=38696353,
)

# Keyed by the exact file name the operator's model directory must contain. A name that is not
# a key here has no audit record and is never loaded, whatever it is called.
REGISTRY: dict[str, FaceModelArtifact] = {
    artifact.file_name: artifact for artifact in (YUNET, SFACE)
}


def digest_of(path: Path) -> str:
    """SHA-256 of a file read in bounded chunks. The caller has already size-bounded it."""
    hashed = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def resolve(model_dir: Path, artifact: FaceModelArtifact) -> Path:
    """Return the verified path of ``artifact`` inside ``model_dir``, or raise.

    The file name comes from the registry, never from configuration or a request, so no path
    component is caller-controlled. Each component is checked for a symlink so a link inside
    the model directory cannot point the loader at a file outside it, and the digest is
    verified before the path is handed to any model loader.
    """
    try:
        root = model_dir.resolve(strict=True)
    except (OSError, RuntimeError):
        raise FaceModelError("model_dir_unavailable") from None
    if not root.is_dir():
        raise FaceModelError("model_dir_unavailable")
    candidate = root / artifact.file_name
    if candidate.is_symlink():
        raise FaceModelError("model_symlink_rejected")
    try:
        status = candidate.stat()
    except OSError:
        raise FaceModelError("model_missing") from None
    if not candidate.is_file():
        raise FaceModelError("model_not_a_regular_file")
    if status.st_size > MAX_MODEL_BYTES or status.st_size != artifact.byte_size:
        raise FaceModelError("model_size_mismatch")
    if digest_of(candidate) != artifact.sha256:
        raise FaceModelError("model_digest_mismatch")
    return candidate


def provenance(artifact: FaceModelArtifact) -> dict[str, str | int]:
    """The audit record of one artifact, for documentation and the manifest test. Safe to
    print: it contains evidence about the model, never anything derived from a person."""
    return {
        "key": artifact.key,
        "role": artifact.role,
        "file_name": artifact.file_name,
        "model_id": artifact.model_id,
        "model_version": artifact.model_version,
        "upstream_repository": artifact.upstream_repository,
        "upstream_revision": artifact.upstream_revision,
        "upstream_url": artifact.upstream_url,
        "license_identifier": artifact.license_identifier,
        "license_evidence_url": artifact.license_evidence_url,
        "training_data_provenance": artifact.training_data_provenance,
        "commercial_use": artifact.commercial_use,
        "approval": artifact.approval,
        "approval_evidence_url": artifact.approval_evidence_url,
        "sha256": artifact.sha256,
        "byte_size": artifact.byte_size,
    }
