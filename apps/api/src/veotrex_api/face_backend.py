"""Face enrollment and recognition backend boundary (V1-02A, extended by V1-02B0).

Business logic never depends on a face-recognition library. It talks to this protocol, which
answers two questions about an already-validated, canonically re-encoded enrollment image: how
many faces it shows and how large the face is, and what template represents that face.

Three implementations ship:

``FakeFaceBackend``
    Deterministic and dependency-free, for CI, local development and the fixture-driven
    tests. It derives a face count from the image's mean colour and a template from a hash of
    the image bytes. Its templates are NOT biometric data and must never be treated as such;
    settings refuse it outside test/local environments.

``UnavailableFaceBackend``
    The production default until a face model with a licence acceptable for VeoTrex has been
    reviewed and adopted (see ADR 0019). It fails closed: every upload is refused with the
    bounded category ``face_backend_unavailable``, so no enrollment image is ever accepted
    without validation and no template is ever fabricated.

``OpenCvEvalFaceBackend`` (``veotrex_api.face_opencv``, V1-02B0)
    Real face detection and real templates from audited OpenCV Zoo weights, for local
    qualification with consenting adults only. It implements both protocols below and is
    refused outside local/development/test/ci by settings and by its own guard.

No implementation here downloads anything.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, TypeGuard

from PIL import Image

from veotrex_api.face_matching import Vector, encode, normalize

MIN_FACE_SIZE_PX = 80


class FaceBackendError(Exception):
    """A bounded category; never model internals, never image or template bytes."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class FaceObservation:
    face_count: int
    face_size_px: int | None
    quality: int | None


@dataclass(frozen=True, slots=True, repr=False)
class FaceTemplate:
    model_id: str
    model_version: str
    template_version: int
    dimensions: int
    dtype: str
    data: bytes
    quality: int | None

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "FaceTemplate(REDACTED)"


class FaceEnrollmentBackend(Protocol):
    model_id: str
    model_version: str
    template_version: int

    @property
    def ready(self) -> bool: ...

    def analyze(self, image: Image.Image) -> FaceObservation: ...

    def extract_template(self, image: Image.Image) -> FaceTemplate: ...


@dataclass(frozen=True, slots=True, repr=False)
class FaceQuery:
    """One query face, ready to compare. ``vector`` is L2-normalised; it is biometric material
    and is never logged, never persisted and never returned through an API."""

    vector: Vector
    face_size_px: int
    quality: int

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "FaceQuery(REDACTED)"


class FaceRecognitionBackend(Protocol):
    """Recognition is a separate capability from enrollment: a backend may be able to produce
    enrollment templates without being allowed to answer "who is this", and the recognition
    surface is gated on its own. Implementations must apply the same detection and validation
    rules to a query image as to an enrollment image."""

    model_id: str
    model_version: str
    template_version: int

    @property
    def ready(self) -> bool: ...

    def extract_query(self, image: Image.Image) -> FaceQuery: ...


def reject_unusable_face(observation: FaceObservation) -> None:
    """Raise the bounded rejection category for a face that cannot be enrolled or compared.

    Enrollment and recognition both call this, so the two surfaces cannot drift apart and a
    caller cannot receive a category one of them knows about and the other does not. Every
    category raised here is in ``staff_service.REJECTION_CATEGORIES``.
    """
    if observation.face_count == 0:
        raise FaceBackendError("no_face_detected")
    if observation.face_count > 1:
        raise FaceBackendError("multiple_faces")
    if observation.face_size_px is not None and observation.face_size_px < MIN_FACE_SIZE_PX:
        raise FaceBackendError("face_too_small")


def supports_recognition(backend: object) -> TypeGuard[FaceRecognitionBackend]:
    """Whether a backend can answer "who is this".

    A structural check rather than ``isinstance``: these protocols carry data members
    (``model_id`` and the versions), which a runtime-checkable Protocol cannot test, and an
    ``isinstance`` that silently only verified the methods would be a weaker guarantee wearing
    a stronger name.
    """
    return callable(getattr(backend, "extract_query", None))


class UnavailableFaceBackend:
    model_id = "none"
    model_version = "none"
    template_version = 0

    @property
    def ready(self) -> bool:
        return False

    def analyze(self, image: Image.Image) -> FaceObservation:
        raise FaceBackendError("face_backend_unavailable")

    def extract_template(self, image: Image.Image) -> FaceTemplate:
        raise FaceBackendError("face_backend_unavailable")

    # Deliberately no ``extract_query``: this backend cannot recognise anyone, and
    # ``supports_recognition`` must be able to say so. An implementation that merely raised
    # would satisfy the structural check and let a recognition route be registered against a
    # backend that can never answer.


class FakeFaceBackend:
    """Deterministic stand-in. Mean colour decides the face count so fixtures are trivial:
    predominantly red means no face, predominantly blue means two faces, anything else one
    face whose size is half the shorter side. The template is a 128-float32 vector derived
    from a SHA-256 of the pixel bytes: identical images give identical templates."""

    model_id = "fake-face"
    model_version = "0"
    template_version = 1
    dimensions = 128

    @property
    def ready(self) -> bool:
        return True

    @staticmethod
    def _mean_rgb(image: Image.Image) -> tuple[float, float, float]:
        raw = image.convert("RGB").resize((16, 16)).tobytes()
        count = max(len(raw) // 3, 1)
        return (sum(raw[0::3]) / count, sum(raw[1::3]) / count, sum(raw[2::3]) / count)

    def analyze(self, image: Image.Image) -> FaceObservation:
        red, green, blue = self._mean_rgb(image)
        if red >= 200 and green < 100 and blue < 100:
            return FaceObservation(0, None, None)
        if blue >= 200 and red < 100 and green < 100:
            return FaceObservation(2, None, None)
        size = min(image.width, image.height) // 4
        return FaceObservation(1, size, 80)

    def _vector(self, image: Image.Image) -> Vector:
        """A deterministic unit vector from the pixel bytes. Normalised like a real backend's,
        so the matching code under test is the same code that runs against a real model;
        identical images give identical vectors and different images give near-orthogonal
        ones, which is what the recognition tests need to steer."""
        digest = hashlib.sha256(image.convert("RGB").tobytes()).digest()
        values: list[float] = []
        seed = digest
        while len(values) < self.dimensions:
            seed = hashlib.sha256(seed).digest()
            values.extend(
                int.from_bytes(seed[index : index + 2], "big") / 65535.0 - 0.5
                for index in range(0, 32, 2)
            )
        return normalize(values[: self.dimensions])

    def extract_template(self, image: Image.Image) -> FaceTemplate:
        observation = self.analyze(image)
        reject_unusable_face(observation)
        return FaceTemplate(
            self.model_id,
            self.model_version,
            self.template_version,
            self.dimensions,
            "float32",
            encode(self._vector(image)),
            observation.quality,
        )

    def extract_query(self, image: Image.Image) -> FaceQuery:
        observation = self.analyze(image)
        reject_unusable_face(observation)
        return FaceQuery(
            self._vector(image),
            observation.face_size_px or MIN_FACE_SIZE_PX,
            observation.quality or 0,
        )


def backend_for(name: str) -> FaceEnrollmentBackend:
    if name == "fake":
        return FakeFaceBackend()
    return UnavailableFaceBackend()
