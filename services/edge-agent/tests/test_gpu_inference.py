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


def pin_jetson_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Present the qualified Jetson runtime identity to verify_engine().

    Only the two platform-identity boundaries are pinned, and only inside a test: the architecture
    and L4T gates in verify_engine() still execute and still decide. Production is never patched,
    so an x86 host continues to be refused. Real engine execution - TensorRT, CUDA, NVDEC - is
    qualified on the Jetson, never here.
    """
    release = tmp_path / "nv_tegra_release"
    release.write_text(f"{runtime_module.L4T_RELEASE_MARKER}, GCID: 0, BOARD: generic\n")
    monkeypatch.setattr(runtime_module, "_machine", lambda: runtime_module.EXPECTED_ARCHITECTURE)
    monkeypatch.setattr(runtime_module, "L4T_RELEASE_PATH", release)


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
            "l4t": runtime_module.EXPECTED_L4T,
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
    """The trusted-artifact contract: path, manifest, hash, size, metadata, declared platform."""
    engine, _ = trusted_store(tmp_path, monkeypatch)
    pin_jetson_platform(tmp_path, monkeypatch)
    assert verify_engine("yolox-s-fp16", root=tmp_path)[0] == engine


# ------------------------------------------------------- the runtime gates the manifest cannot
def test_non_aarch64_runtime_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine is qualified for the Jetson stack; no x86 host may classify it as executable."""
    trusted_store(tmp_path, monkeypatch)
    pin_jetson_platform(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime_module, "_machine", lambda: "x86_64")
    with pytest.raises(RuntimeFailure, match="platform_incompatible"):
        verify_engine("yolox-s-fp16", root=tmp_path)


def test_absent_or_wrong_l4t_release_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest declaring aarch64 is not evidence the host runs the qualified L4T release."""
    trusted_store(tmp_path, monkeypatch)
    pin_jetson_platform(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime_module, "L4T_RELEASE_PATH", tmp_path / "absent")
    with pytest.raises(RuntimeFailure, match="platform_incompatible"):
        verify_engine("yolox-s-fp16", root=tmp_path)
    wrong = tmp_path / "wrong_release"
    wrong.write_text("# R36 (release), REVISION: 4.0\n")
    monkeypatch.setattr(runtime_module, "L4T_RELEASE_PATH", wrong)
    with pytest.raises(RuntimeFailure, match="platform_incompatible"):
        verify_engine("yolox-s-fp16", root=tmp_path)


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
                "l4t": runtime_module.EXPECTED_L4T,
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


# ---------------------------------------------- R39.2.1 qualification (hotfix, 2026-09-25)
PINNED_ONNX_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
R39_2_1_RELEASE = (
    "# R39 (release), REVISION: 2.1, GCID: 46758480, BOARD: generic, EABI: aarch64, "
    "DATE: Fri Aug  7 05:54:22 AM UTC 2026\n"
    "# KERNEL_VARIANT: oot\n"
)
R39_2_0_RELEASE = R39_2_1_RELEASE.replace("REVISION: 2.1,", "REVISION: 2.0,")


def _host_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    release = tmp_path / "host_nv_tegra_release"
    release.write_text(text)
    monkeypatch.setattr(runtime_module, "_machine", lambda: "aarch64")
    monkeypatch.setattr(runtime_module, "L4T_RELEASE_PATH", release)


def _edit_manifest(manifest_path: Path, **changes: object) -> None:
    manifest = json.loads(manifest_path.read_text())
    for key, value in changes.items():
        if key in {"architecture", "cuda", "l4t", "tensorrt"}:
            manifest["compatibility"][key] = value
        else:
            manifest[key] = value
    manifest_path.write_text(json.dumps(manifest))


def test_the_qualified_platform_is_exactly_r39_2_1() -> None:
    assert runtime_module.EXPECTED_L4T == "39.2.1"
    assert runtime_module.L4T_RELEASE_MARKER == "# R39 (release), REVISION: 2.1"
    assert runtime_module.EXPECTED_TRT == "10.16.2.10"
    assert runtime_module.EXPECTED_ARCHITECTURE == "aarch64"
    # The rebuilt plan is a new artifact of the same pinned parent.
    assert runtime_module.ONNX_SHA256 == PINNED_ONNX_SHA256
    assert runtime_module.ENGINE_SHA256 != (
        "f204dff3573a15647266ba287f789d266fd95a912dd0a75e973ab046e3991068"
    ), "the R39.2.0 plan must not be relabelled as R39.2.1"


def test_r39_2_1_manifest_on_r39_2_1_host_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = trusted_store(tmp_path, monkeypatch)
    _host_release(tmp_path, monkeypatch, R39_2_1_RELEASE)
    resolved, contract = verify_engine("yolox-s-fp16", root=tmp_path)
    assert resolved == engine and contract["parent_onnx_sha256"] == PINNED_ONNX_SHA256


def test_r39_2_0_manifest_on_r39_2_1_host_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest_path = trusted_store(tmp_path, monkeypatch)
    _edit_manifest(manifest_path, l4t="39.2.0")
    _host_release(tmp_path, monkeypatch, R39_2_1_RELEASE)
    with pytest.raises(RuntimeFailure, match="platform_incompatible"):
        verify_engine("yolox-s-fp16", root=tmp_path)


def test_r39_2_1_manifest_on_r39_2_0_host_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_store(tmp_path, monkeypatch)
    _host_release(tmp_path, monkeypatch, R39_2_0_RELEASE)
    with pytest.raises(RuntimeFailure, match="platform_incompatible"):
        verify_engine("yolox-s-fp16", root=tmp_path)


@pytest.mark.parametrize(
    "release",
    [
        R39_2_1_RELEASE.replace("REVISION: 2.1,", "REVISION: 2.10,"),
        R39_2_1_RELEASE.replace("REVISION: 2.1,", "REVISION: 2.11,"),
        R39_2_1_RELEASE.replace("# R39", "# R40"),
        R39_2_1_RELEASE.replace("# R39", "# R3"),
        "# R39 (release), REVISION: 2.1x\n",
        "junk\n# R39 (release), REVISION: 2.0, GCID: 1\n",
        "",
    ],
)
def test_only_the_exact_l4t_revision_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: str
) -> None:
    trusted_store(tmp_path, monkeypatch)
    _host_release(tmp_path, monkeypatch, release)
    with pytest.raises(RuntimeFailure, match="platform_incompatible"):
        verify_engine("yolox-s-fp16", root=tmp_path)


def test_the_l4t_marker_may_end_the_line() -> None:
    assert runtime_module.l4t_release_matches("# R39 (release), REVISION: 2.1\n")
    assert runtime_module.l4t_release_matches(R39_2_1_RELEASE)
    assert not runtime_module.l4t_release_matches(R39_2_0_RELEASE)


@pytest.mark.parametrize(
    ("changes", "category"),
    [
        ({"architecture": "x86_64"}, "platform_incompatible"),
        ({"tensorrt": "10.16.2.9"}, "platform_incompatible"),
        ({"cuda": "13.0"}, "platform_incompatible"),
        ({"l4t": "39.2"}, "platform_incompatible"),
        ({"parent_onnx_sha256": "0" * 64}, "manifest_contract_mismatch"),
        ({"precision": "fp32"}, "manifest_contract_mismatch"),
        ({"artifact_status": "candidate"}, "manifest_contract_mismatch"),
        (
            {"outputs": [{"dtype": "float32", "name": "output", "shape": [1, 8400, 84]}]},
            "manifest_contract_mismatch",
        ),
        (
            {"inputs": [{"dtype": "float32", "name": "input", "shape": [1, 3, 640, 640]}]},
            "manifest_contract_mismatch",
        ),
        ({"byte_size": 5}, "artifact_size_mismatch"),
        ({"sha256": "1" * 64}, "artifact_hash_mismatch"),
    ],
)
def test_every_other_contract_field_still_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
    category: str,
) -> None:
    _, manifest_path = trusted_store(tmp_path, monkeypatch)
    _edit_manifest(manifest_path, **changes)
    _host_release(tmp_path, monkeypatch, R39_2_1_RELEASE)
    with pytest.raises(RuntimeFailure, match=category):
        verify_engine("yolox-s-fp16", root=tmp_path)


def test_the_runtime_constants_bind_size_and_digest_not_just_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-consistent manifest for a different plan is still refused by the constants."""
    engine, manifest_path = trusted_store(tmp_path, monkeypatch)
    engine.write_bytes(b"other")
    _edit_manifest(
        manifest_path, sha256=hashlib.sha256(b"other").hexdigest(), byte_size=len(b"other")
    )
    _host_release(tmp_path, monkeypatch, R39_2_1_RELEASE)
    with pytest.raises(RuntimeFailure, match="manifest_contract_mismatch"):
        verify_engine("yolox-s-fp16", root=tmp_path)


def test_a_symlinked_directory_or_outside_root_engine_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    engine, manifest_path = trusted_store(store, monkeypatch)
    _host_release(tmp_path, monkeypatch, R39_2_1_RELEASE)
    outside = tmp_path / "outside_engines"
    engine.parent.rename(outside)
    (store / "yolox-s" / "engines").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeFailure) as caught:
        verify_engine("yolox-s-fp16", root=store)
    assert str(caught.value) in {"artifact_symlink_rejected", "artifact_outside_trusted_root"}
    assert manifest_path.name.endswith(".plan.manifest.json")


# -------------------------------------------- detector surfaces bounded worker categories
class _FailingSupervisor:
    failure: BaseException = RuntimeError("unset")
    stopped = 0

    def start(self) -> dict[str, object]:
        return {}

    def load_model(self, model_id: str = "yolox-s-fp16") -> dict[str, object]:
        raise type(self).failure

    def stop(self) -> None:
        type(self).stopped += 1


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        ("platform_incompatible", "gpu_worker_platform_incompatible"),
        ("tensorrt_version_mismatch", "gpu_worker_tensorrt_version_mismatch"),
        ("artifact_hash_mismatch", "gpu_worker_artifact_hash_mismatch"),
        ("engine_tensor_contract_mismatch", "gpu_worker_engine_tensor_contract_mismatch"),
        ("worker_error", "gpu_worker_start_failed"),
        ("inference_failed", "gpu_worker_start_failed"),
        ("/opt/secret/path.plan", "gpu_worker_start_failed"),
    ],
)
def test_the_detector_surfaces_only_allowlisted_worker_categories(
    monkeypatch: pytest.MonkeyPatch, failure: str, category: str
) -> None:
    from veotrex_edge_agent import gpu_worker
    from veotrex_edge_agent.gpu_worker.supervisor import WorkerFailure
    from veotrex_edge_agent.recorded.yolox import DetectorUnavailable, YoloxPersonDetector

    _FailingSupervisor.failure = WorkerFailure(failure)
    _FailingSupervisor.stopped = 0
    monkeypatch.setattr(gpu_worker, "GpuWorkerSupervisor", _FailingSupervisor)
    with pytest.raises(DetectorUnavailable) as caught:
        YoloxPersonDetector(environment="test").start()
    assert str(caught.value) == category
    assert _FailingSupervisor.stopped == 1, "a failed load never leaves the worker running"


def test_non_worker_exceptions_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    from veotrex_edge_agent import gpu_worker
    from veotrex_edge_agent.recorded.yolox import DetectorUnavailable, YoloxPersonDetector

    _FailingSupervisor.failure = RuntimeError(
        "platform_incompatible at /home/x/.env VEOTREX_SECRET=abc"
    )
    monkeypatch.setattr(gpu_worker, "GpuWorkerSupervisor", _FailingSupervisor)
    with pytest.raises(DetectorUnavailable) as caught:
        YoloxPersonDetector(environment="test").start()
    assert str(caught.value) == "gpu_worker_start_failed"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_every_surfaced_category_is_one_the_supervisor_can_produce() -> None:
    from veotrex_edge_agent.gpu_worker import supervisor
    from veotrex_edge_agent.recorded.yolox import SURFACED_WORKER_FAILURES

    source = Path(supervisor.__file__).read_text()
    for category in SURFACED_WORKER_FAILURES:
        assert f'"{category}"' in source, category
