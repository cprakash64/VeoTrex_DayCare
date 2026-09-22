"""Face enrollment backend boundary (V1-02A).

Business logic never depends on a face-recognition library. It talks to this protocol, which
answers two questions about an already-validated, canonically re-encoded enrollment image: how
many faces it shows and how large the face is, and what template represents that face.

Two implementations ship:

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

No implementation here downloads anything.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Protocol

from PIL import Image

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

    def extract_template(self, image: Image.Image) -> FaceTemplate:
        observation = self.analyze(image)
        if observation.face_count != 1:
            raise FaceBackendError("no_single_face")
        digest = hashlib.sha256(image.convert("RGB").tobytes()).digest()
        values: list[float] = []
        seed = digest
        while len(values) < self.dimensions:
            seed = hashlib.sha256(seed).digest()
            values.extend(
                int.from_bytes(seed[index : index + 2], "big") / 65535.0
                for index in range(0, 32, 2)
            )
        data = struct.pack(f"<{self.dimensions}f", *values[: self.dimensions])
        return FaceTemplate(
            self.model_id,
            self.model_version,
            self.template_version,
            self.dimensions,
            "float32",
            data,
            observation.quality,
        )


def backend_for(name: str) -> FaceEnrollmentBackend:
    if name == "fake":
        return FakeFaceBackend()
    return UnavailableFaceBackend()
