"""Enrollment image validation and the private media store (V1-02A). No database."""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from veotrex_api.face_backend import FaceBackendError, FakeFaceBackend, UnavailableFaceBackend
from veotrex_api.staff_media import (
    MAX_PIXELS,
    EnrollmentImageRejected,
    StaffMediaStore,
    sniff_media_type,
    validate_enrollment_image,
)

MAX_BYTES = 2_000_000


def solid(
    color: tuple[int, int, int], size: tuple[int, int] = (400, 400), fmt: str = "JPEG"
) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    if fmt == "JPEG":
        exif = Image.Exif()
        exif[0x0112] = 6  # orientation: rotate 90
        exif[0x9286] = "user comment that must not survive"
        image.save(buffer, format="JPEG", quality=85, exif=exif.tobytes(), comment=b"strip me")
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


def png_with_declared_size(width: int, height: int) -> bytes:
    """A syntactically valid PNG header declaring a huge canvas with no pixel data."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def test_sniffing_uses_bytes_not_names() -> None:
    assert sniff_media_type(solid((128, 128, 128))) == "image/jpeg"
    assert sniff_media_type(solid((128, 128, 128), fmt="PNG")) == "image/png"
    assert sniff_media_type(b"GIF89a" + b"\x00" * 32) is None
    assert sniff_media_type(b"<html>") is None
    assert sniff_media_type(b"") is None


def test_valid_jpeg_and_png_are_canonicalised_without_metadata() -> None:
    for fmt in ("JPEG", "PNG"):
        validated = validate_enrollment_image(
            solid((120, 130, 140), (640, 480), fmt), max_bytes=MAX_BYTES
        )
        assert validated.width > 0 and validated.height > 0
        assert validated.canonical_bytes.startswith(b"\xff\xd8\xff")
        assert b"strip me" not in validated.canonical_bytes
        assert b"user comment" not in validated.canonical_bytes
        assert b"Exif" not in validated.canonical_bytes
        with Image.open(io.BytesIO(validated.canonical_bytes)) as reopened:
            assert reopened.format == "JPEG" and reopened.mode == "RGB"
            assert not reopened.getexif()


def test_exif_orientation_is_applied_to_the_canonical_copy() -> None:
    # 640x480 with orientation 6 (rotate 90) becomes 480x640 after transposition.
    validated = validate_enrollment_image(solid((10, 20, 30), (640, 480)), max_bytes=MAX_BYTES)
    assert (validated.width, validated.height) == (480, 640)


def test_large_images_are_downscaled_to_the_canonical_bound() -> None:
    validated = validate_enrollment_image(
        solid((10, 20, 30), (3000, 2000), "PNG"), max_bytes=50_000_000
    )
    assert max(validated.width, validated.height) == 1600


@pytest.mark.parametrize(
    ("data", "category"),
    [
        (b"", "empty_upload"),
        (b"GIF89a" + b"\x00" * 200, "unsupported_type"),
        (b"\xff\xd8\xff" + b"not really a jpeg" * 40, "invalid_image"),
        (solid((10, 20, 30))[:800], "invalid_image"),
        (solid((10, 20, 30), (100, 100)), "image_too_small"),
        (png_with_declared_size(20_000, 20_000), "image_too_large"),
        (png_with_declared_size(6_000, 6_000), "image_too_large"),
    ],
)
def test_rejections_are_bounded_categories(data: bytes, category: str) -> None:
    with pytest.raises(EnrollmentImageRejected) as caught:
        validate_enrollment_image(data, max_bytes=MAX_BYTES)
    assert caught.value.category == category
    assert 6_000 * 6_000 > MAX_PIXELS


def test_file_size_cap_is_enforced_before_decoding() -> None:
    data = solid((10, 20, 30), (800, 800))
    with pytest.raises(EnrollmentImageRejected, match="file_too_large"):
        validate_enrollment_image(data, max_bytes=len(data) - 1)


def test_media_store_is_opaque_atomic_private_and_traversal_proof(tmp_path: Path) -> None:
    store = StaffMediaStore(tmp_path / "media")
    store.ensure_ready()
    assert oct((tmp_path / "media").stat().st_mode & 0o777) == "0o700"
    tenant = uuid4()
    key = store.put(tenant, b"\xff\xd8\xffcanonical")
    assert len(key) == 32 and key.isalnum()
    path = tmp_path / "media" / str(tenant) / f"{key}.jpg"
    assert path.read_bytes() == b"\xff\xd8\xffcanonical"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not list((tmp_path / "media" / str(tenant)).glob(".*.tmp"))
    assert store.get(tenant, key) == b"\xff\xd8\xffcanonical"
    assert store.get(uuid4(), key) is None, "another tenant cannot read by key"
    for bad in ("../../etc/passwd", "..", "", key.upper() + "Z", "x" * 32, f"{key}/../{key}"):
        with pytest.raises(EnrollmentImageRejected, match="invalid_media_key"):
            store.get(tenant, bad)
    store.delete(tenant, key)
    assert store.get(tenant, key) is None
    store.delete(tenant, key)  # idempotent


def test_fake_backend_is_deterministic_and_the_unavailable_backend_fails_closed() -> None:
    fake = FakeFaceBackend()
    with Image.open(io.BytesIO(solid((128, 128, 128)))) as one:
        assert fake.analyze(one).face_count == 1
        first = fake.extract_template(one)
        second = fake.extract_template(one)
    assert first.data == second.data and len(first.data) == 128 * 4 and first.dtype == "float32"
    assert "REDACTED" in repr(first) and first.data[:8] not in repr(first).encode()
    # V1-02B0: extraction refuses with the same bounded categories the enrollment API exposes,
    # rather than a private "no single face" that no caller has a message for.
    with Image.open(io.BytesIO(solid((250, 10, 10)))) as none:
        assert fake.analyze(none).face_count == 0
        with pytest.raises(FaceBackendError, match="no_face_detected"):
            fake.extract_template(none)
    with Image.open(io.BytesIO(solid((10, 10, 250)))) as two:
        assert fake.analyze(two).face_count == 2
        with pytest.raises(FaceBackendError, match="multiple_faces"):
            fake.extract_template(two)
    unavailable = UnavailableFaceBackend()
    assert unavailable.ready is False
    with Image.open(io.BytesIO(solid((128, 128, 128)))) as image:
        with pytest.raises(FaceBackendError, match="face_backend_unavailable"):
            unavailable.analyze(image)
