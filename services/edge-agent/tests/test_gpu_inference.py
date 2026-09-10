from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import socket
from pathlib import Path

import pytest

from veotrex_edge_agent.gpu_worker import runtime as runtime_module
from veotrex_edge_agent.gpu_worker.decoder import (
    YOLOX_FEATURES,
    YOLOX_ROWS,
    Candidate,
    DetectionConfig,
    decode_person_candidates,
    grid_position,
    non_maximum_suppression,
)
from veotrex_edge_agent.gpu_worker.fd_transport import (
    REQUIRED_SEALS,
    FileDescriptorError,
    create_sealed_memfd,
    receive_packet,
    send_packet,
    validate_sealed_memfd,
)
from veotrex_edge_agent.gpu_worker.runtime import INPUT_BYTES, RuntimeFailure, verify_engine


def output() -> list[float]:
    return [0.0] * (YOLOX_ROWS * YOLOX_FEATURES)


def row(
    values: list[float],
    index: int,
    *,
    xywh: tuple[float, float, float, float],
    obj: float,
    person: float,
) -> None:
    offset = index * YOLOX_FEATURES
    values[offset : offset + 6] = [*xywh, obj, person]


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (0, (0, 0, 8)),
        (6399, (79, 79, 8)),
        (6400, (0, 0, 16)),
        (7999, (39, 39, 16)),
        (8000, (0, 0, 32)),
        (8399, (19, 19, 32)),
    ],
)
def test_grid_order_and_strides(index: int, expected: tuple[int, int, int]) -> None:
    assert grid_position(index) == expected


def test_decode_math_person_score_and_clipping() -> None:
    values = output()
    row(values, 6482, xywh=(0.5, 0.25, math.log(2), 0), obj=0.8, person=0.5)
    candidates, anomalies = decode_person_candidates(values)
    assert anomalies == 0
    # Row 6482 is local grid (2, 2) at stride 16.
    assert candidates == [Candidate(24.0, 28.0, 56.0, 44.0, 0.4, 6482)]


def test_threshold_is_inclusive_and_person_only() -> None:
    values = output()
    row(values, 0, xywh=(0.5, 0.5, 0, 0), obj=0.5, person=0.5)
    candidates, _ = decode_person_candidates(values)
    assert len(candidates) == 1 and candidates[0].score == 0.25


def test_decoder_drops_nonfinite_overflow_and_zero_area() -> None:
    values = output()
    row(values, 0, xywh=(math.nan, 0, 0, 0), obj=1, person=1)
    row(values, 1, xywh=(0, 0, 1000, 0), obj=1, person=1)
    row(values, 2, xywh=(-100, -100, 0, 0), obj=1, person=1)
    candidates, anomalies = decode_person_candidates(values)
    assert candidates == [] and anomalies == 3


def test_top_k_and_tie_order_are_bounded() -> None:
    values = output()
    for index in range(5):
        row(values, index, xywh=(0.5, 0.5, -2, -2), obj=1, person=0.9)
    candidates, _ = decode_person_candidates(
        values, DetectionConfig(pre_nms_max_candidates=3, post_nms_max_detections=2)
    )
    assert [item.source_index for item in candidates] == [0, 1, 2]


def candidate(x1: float, y1: float, x2: float, y2: float, score: float, index: int) -> Candidate:
    return Candidate(x1, y1, x2, y2, score, index)


def test_nms_empty_one_overlap_nonoverlap_and_identical() -> None:
    assert non_maximum_suppression([]) == []
    a = candidate(0, 0, 10, 10, 0.9, 0)
    b = candidate(1, 1, 11, 11, 0.8, 1)
    c = candidate(20, 20, 30, 30, 0.7, 2)
    d = candidate(0, 0, 10, 10, 0.6, 3)
    assert non_maximum_suppression([d, c, b, a]) == [a, c]


def test_nms_threshold_boundary_is_retained() -> None:
    a = candidate(0, 0, 10, 10, 0.9, 0)
    b = candidate(5, 0, 15, 10, 0.8, 1)  # IoU exactly 1/3.
    config = DetectionConfig(nms_iou_threshold=1 / 3)
    assert non_maximum_suppression([a, b], config) == [a, b]


def test_nms_equal_score_tie_max_and_invalid() -> None:
    invalid = candidate(3, 3, 2, 2, 1.0, 9)
    valid = [candidate(i * 20, 0, i * 20 + 10, 10, 0.9, i) for i in range(4)]
    config = DetectionConfig(pre_nms_max_candidates=3, post_nms_max_detections=2)
    assert non_maximum_suppression([invalid, *reversed(valid)], config) == valid[:2]


def test_sealed_memfd_valid_size_and_seals() -> None:
    fd = create_sealed_memfd(b"abcd")
    try:
        validate_sealed_memfd(fd, 4)
        assert fcntl.fcntl(fd, fcntl.F_GET_SEALS) & REQUIRED_SEALS == REQUIRED_SEALS
    finally:
        os.close(fd)


def test_mutable_and_wrong_size_memfd_rejected() -> None:
    fd = os.memfd_create("mutable", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    os.write(fd, b"abcd")
    try:
        with pytest.raises(FileDescriptorError, match="mutable_tensor_rejected"):
            validate_sealed_memfd(fd, 4)
        with pytest.raises(FileDescriptorError, match="invalid_tensor_size"):
            validate_sealed_memfd(fd, 5)
    finally:
        os.close(fd)


def test_fd_passes_once_and_sender_retains_ownership() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    fd = create_sealed_memfd(b"abcd")
    try:
        send_packet(left, b"metadata", (fd,))
        data, received = receive_packet(right, 32)
        assert data == b"metadata" and len(received) == 1
        validate_sealed_memfd(received[0], 4)
        os.close(received[0])
        os.fstat(fd)
    finally:
        os.close(fd)
        left.close()
        right.close()


def test_verify_engine_rejects_unknown_identity_and_missing_store(tmp_path: Path) -> None:
    with pytest.raises(RuntimeFailure, match="unknown_model_id"):
        verify_engine("../../arbitrary.plan", root=tmp_path)
    with pytest.raises(RuntimeFailure, match="artifact_unavailable"):
        verify_engine("yolox-s-fp16", root=tmp_path)


def test_input_contract_byte_size_is_exact() -> None:
    assert INPUT_BYTES == 1 * 3 * 640 * 640 * 4


def trusted_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    engine = tmp_path / "yolox-s" / "engines" / "yolox_s_fp16_a.plan"
    engine.parent.mkdir(parents=True)
    engine.write_bytes(b"plan")
    digest = hashlib.sha256(b"plan").hexdigest()
    monkeypatch.setattr(runtime_module, "ENGINE_SIZE", 4)
    monkeypatch.setattr(runtime_module, "ENGINE_SHA256", digest)
    manifest = {
        "schema_version": 1,
        "artifact_type": "plan",
        "artifact_status": "qualified",
        "model_family": "YOLOX",
        "model_variant": "YOLOX-S",
        "precision": "fp16",
        "sha256": digest,
        "byte_size": 4,
        "parent_onnx_sha256": runtime_module.ONNX_SHA256,
        "inputs": [{"dtype": "float32", "name": "images", "shape": [1, 3, 640, 640]}],
        "outputs": [{"dtype": "float32", "name": "output", "shape": [1, 8400, 85]}],
        "compatibility": {
            "architecture": "aarch64",
            "cuda": "13.2",
            "l4t": "39.2.0",
            "tensorrt": "10.16.2.10",
        },
        "upstream_url": "https://example.invalid/model.onnx",
        "upstream_repository": "https://example.invalid/repository",
        "upstream_revision": "test",
        "retrieved_at": None,
        "license_identifier": "test-only",
        "license_evidence_url": "https://example.invalid/license",
        "onnx_opset": 11,
        "engine_build": {"locally_built": True},
    }
    manifest_path = engine.with_suffix(".plan.manifest.json")
    manifest_path.write_text(json.dumps(manifest))
    return engine, manifest_path


def test_valid_locally_approved_manifest_and_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = trusted_store(tmp_path, monkeypatch)
    assert verify_engine("yolox-s-fp16", root=tmp_path)[0] == engine


def test_wrong_engine_hash_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = trusted_store(tmp_path, monkeypatch)
    engine.write_bytes(b"evil")
    with pytest.raises(RuntimeFailure, match="artifact_hash_mismatch"):
        verify_engine("yolox-s-fp16", root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_variant", "YOLOX-Tiny"),
        ("inputs", [{"dtype": "float16", "name": "images", "shape": [1, 3, 640, 640]}]),
        (
            "compatibility",
            {
                "architecture": "aarch64",
                "cuda": "13.2",
                "l4t": "39.2.0",
                "tensorrt": "wrong",
            },
        ),
    ],
)
def test_manifest_identity_tensor_and_compatibility_mismatch_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    _, manifest_path = trusted_store(tmp_path, monkeypatch)
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest))
    category = "platform_incompatible" if field == "compatibility" else "manifest_contract_mismatch"
    with pytest.raises(RuntimeFailure, match=category):
        verify_engine("yolox-s-fp16", root=tmp_path)


def test_engine_symlink_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = trusted_store(tmp_path, monkeypatch)
    outside = tmp_path / "outside.plan"
    outside.write_bytes(b"plan")
    engine.unlink()
    engine.symlink_to(outside)
    with pytest.raises(RuntimeFailure, match="artifact_symlink_rejected"):
        verify_engine("yolox-s-fp16", root=tmp_path)


def test_malformed_non_rights_ancillary_is_rejected() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    right.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    try:
        left.send(b"metadata")
        with pytest.raises(FileDescriptorError, match="malformed_ancillary_data"):
            receive_packet(right, 32)
    finally:
        left.close()
        right.close()


def test_extra_descriptors_are_observable_for_exact_count_rejection() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    first, second = create_sealed_memfd(b"a"), create_sealed_memfd(b"b")
    received: list[int] = []
    try:
        send_packet(left, b"metadata", (first, second))
        _, received = receive_packet(right, 32)
        assert len(received) == 2
    finally:
        for descriptor in received:
            os.close(descriptor)
        os.close(first)
        os.close(second)
        left.close()
        right.close()


def test_repeated_descriptor_transport_does_not_leak() -> None:
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(50):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        descriptor = create_sealed_memfd(b"tensor")
        send_packet(left, b"metadata", (descriptor,))
        _, received = receive_packet(right, 32)
        os.close(received[0])
        os.close(descriptor)
        left.close()
        right.close()
    assert len(os.listdir("/proc/self/fd")) == before
