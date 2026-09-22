"""Enrollment image validation and the private staff media store (V1-02A).

Validation works from bytes, never from a filename or declared media type: the container is
sniffed, the image is decoded by Pillow with a pixel-count cap checked before any pixel data is
read, EXIF orientation is applied, and a canonical JPEG copy is produced with every metadata
block discarded. Only that canonical copy is ever stored or served.

The media store is a private directory owned by the API process. Keys are server-generated,
opaque and validated against a fixed pattern before they touch a path, so no request can name
a filesystem location. Writes are atomic (temporary file, fsync, rename) and files are created
0600. Nothing here logs image bytes.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

# Fail closed on truncated files rather than accepting partial pixel data.
ImageFile.LOAD_TRUNCATED_IMAGES = False

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ALLOWED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
CANONICAL_MEDIA_TYPE = "image/jpeg"
MAX_PIXELS = 25_000_000
MAX_SIDE = 8_000
MIN_SIDE = 160
CANONICAL_MAX_SIDE = 1_600
CANONICAL_JPEG_QUALITY = 90
MEDIA_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class EnrollmentImageRejected(Exception):
    """A bounded, human-readable rejection category; never decoder internals."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True, repr=False)
class ValidatedImage:
    image: Image.Image
    canonical_bytes: bytes
    width: int
    height: int
    content_sha256: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"ValidatedImage({self.width}x{self.height})"


def sniff_media_type(data: bytes) -> str | None:
    if data.startswith(JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(PNG_MAGIC):
        return "image/png"
    return None


def validate_enrollment_image(data: bytes, *, max_bytes: int) -> ValidatedImage:
    """Decode, bound and canonicalise an uploaded image or raise ``EnrollmentImageRejected``."""
    if not data:
        raise EnrollmentImageRejected("empty_upload")
    if len(data) > max_bytes:
        raise EnrollmentImageRejected("file_too_large")
    if sniff_media_type(data) is None:
        raise EnrollmentImageRejected("unsupported_type")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            # Header only so far: bound the dimensions before a single pixel is decoded.
            width, height = probe.size
            declared_format = probe.format
    except Image.DecompressionBombError:
        # Pillow refuses at open time when the declared canvas is absurd (> 2x its limit).
        raise EnrollmentImageRejected("image_too_large") from None
    except (UnidentifiedImageError, OSError, ValueError):
        raise EnrollmentImageRejected("invalid_image") from None
    if declared_format not in {"JPEG", "PNG"}:
        raise EnrollmentImageRejected("unsupported_type")
    if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
        raise EnrollmentImageRejected("image_too_large")
    if width < MIN_SIDE or height < MIN_SIDE:
        raise EnrollmentImageRejected("image_too_small")
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as decoded:
            decoded.load()
            oriented = ImageOps.exif_transpose(decoded) or decoded
            canonical = oriented.convert("RGB")
    except Image.DecompressionBombError:
        raise EnrollmentImageRejected("image_too_large") from None
    except (UnidentifiedImageError, OSError, ValueError):
        raise EnrollmentImageRejected("invalid_image") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    canonical.thumbnail((CANONICAL_MAX_SIDE, CANONICAL_MAX_SIDE))
    if canonical.width < MIN_SIDE or canonical.height < MIN_SIDE:
        raise EnrollmentImageRejected("image_too_small")
    # Pillow carries the source's ``info`` (EXIF, ICC profile, JPEG comment, PNG text chunks)
    # through convert/thumbnail and re-emits it on save. Copy the pixels into a brand-new
    # image so the canonical file contains pixel data and nothing else.
    clean = Image.new("RGB", canonical.size)
    clean.paste(canonical)
    buffer = io.BytesIO()
    clean.save(buffer, format="JPEG", quality=CANONICAL_JPEG_QUALITY, optimize=True)
    canonical = clean
    canonical_bytes = buffer.getvalue()
    return ValidatedImage(
        canonical,
        canonical_bytes,
        canonical.width,
        canonical.height,
        hashlib.sha256(canonical_bytes).hexdigest(),
    )


class StaffMediaStore:
    """Private, tenant-partitioned, opaque-key file store for canonical enrollment images."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def ensure_ready(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._root, 0o700)
        except OSError:  # pragma: no cover - ownership differs on shared volumes
            pass

    @staticmethod
    def new_key() -> str:
        return uuid.uuid4().hex

    def _path(self, tenant_id: UUID, key: str) -> Path:
        if not MEDIA_KEY_PATTERN.fullmatch(key):
            raise EnrollmentImageRejected("invalid_media_key")
        # Both components are fixed-format identifiers; no request-controlled path segment.
        return self._root / str(tenant_id) / f"{key}.jpg"

    def put(self, tenant_id: UUID, data: bytes) -> str:
        key = self.new_key()
        path = self._path(tenant_id, key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{key}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return key

    def get(self, tenant_id: UUID, key: str) -> bytes | None:
        path = self._path(tenant_id, key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def delete(self, tenant_id: UUID, key: str) -> None:
        self._path(tenant_id, key).unlink(missing_ok=True)
