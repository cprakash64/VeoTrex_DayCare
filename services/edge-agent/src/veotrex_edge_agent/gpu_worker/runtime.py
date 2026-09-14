from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import math
import platform
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from veotrex_edge_agent.model_artifacts import (
    ArtifactManifest,
    ArtifactValidationError,
    PlatformCompatibility,
    validate_compatibility,
    verify_artifact,
)

try:
    from . import decoder as decoder_module
except ImportError:  # Standalone worker under /usr/bin/python3 -I.
    import decoder as decoder_module  # type: ignore[import-not-found,no-redef]

YOLOX_FEATURES = decoder_module.YOLOX_FEATURES
YOLOX_ROWS = decoder_module.YOLOX_ROWS

MODEL_ID = "yolox-s-fp16"
ENGINE_SHA256 = "f204dff3573a15647266ba287f789d266fd95a912dd0a75e973ab046e3991068"
ONNX_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
ENGINE_SIZE = 21_356_188
INPUT_NAME = "images"
OUTPUT_NAME = "output"
INPUT_SHAPE = (1, 3, 640, 640)
OUTPUT_SHAPE = (1, 8_400, 85)
INPUT_BYTES = 4_915_200
OUTPUT_BYTES = 2_856_000
EXPECTED_TRT = "10.16.2.10"
EXPECTED_L4T = "39.2.0"
EXPECTED_ARCHITECTURE = "aarch64"
L4T_RELEASE_PATH = Path("/etc/nv_tegra_release")
L4T_RELEASE_MARKER = "# R39 (release), REVISION: 2.0"


def _machine() -> str:
    """The architecture this process is running on.

    Indirected so a test can pin the platform it is qualifying a manifest against. The check that
    consumes it is unconditional: nothing here lets a non-Jetson host load the engine.
    """
    return platform.machine()


class RuntimeFailure(RuntimeError):
    """Sanitized worker runtime failure."""


def _artifact_root() -> Path:
    return Path(__file__).resolve().parents[5] / "artifacts" / "models" / "candidates"


def verify_engine(model_id: str, *, root: Path | None = None) -> tuple[Path, dict[str, Any]]:
    if model_id != MODEL_ID:
        raise RuntimeFailure("unknown_model_id")
    trusted_root = root or _artifact_root()
    engine = trusted_root / "yolox-s" / "engines" / "yolox_s_fp16_a.plan"
    manifest_path = engine.with_suffix(".plan.manifest.json")
    try:
        manifest = ArtifactManifest.load(manifest_path)
        resolved = verify_artifact(trusted_root, engine, manifest)
    except ArtifactValidationError as exc:
        categories = {
            "artifact_hash_mismatch",
            "artifact_size_mismatch",
            "artifact_symlink_rejected",
            "artifact_outside_trusted_root",
        }
        category = str(exc) if str(exc) in categories else "artifact_unavailable"
        raise RuntimeFailure(category) from None
    required = {
        "artifact_type": "plan",
        "artifact_status": "qualified",
        "model_family": "YOLOX",
        "model_variant": "YOLOX-S",
        "precision": "fp16",
        "sha256": ENGINE_SHA256,
        "byte_size": ENGINE_SIZE,
        "parent_onnx_sha256": ONNX_SHA256,
        "inputs": [{"dtype": "float32", "name": INPUT_NAME, "shape": list(INPUT_SHAPE)}],
        "outputs": [{"dtype": "float32", "name": OUTPUT_NAME, "shape": list(OUTPUT_SHAPE)}],
    }
    if any(getattr(manifest, key) != value for key, value in required.items()):
        raise RuntimeFailure("manifest_contract_mismatch")
    expected = PlatformCompatibility(
        architecture="aarch64", cuda="13.2", l4t=EXPECTED_L4T, tensorrt=EXPECTED_TRT
    )
    try:
        validate_compatibility(manifest, expected)
    except ArtifactValidationError:
        raise RuntimeFailure("platform_incompatible") from None
    if _machine() != EXPECTED_ARCHITECTURE:
        raise RuntimeFailure("platform_incompatible")
    try:
        l4t_release = L4T_RELEASE_PATH.read_text(encoding="utf-8")
    except OSError:
        raise RuntimeFailure("platform_incompatible") from None
    if L4T_RELEASE_MARKER not in l4t_release:
        raise RuntimeFailure("platform_incompatible")
    return resolved, {key: getattr(manifest, key) for key in required}


class TensorRtRuntime:
    def __init__(self) -> None:
        self.state = "UNLOADED"
        self.model_id: str | None = None
        self.engine: Any = None
        self.context: Any = None
        self.runtime: Any = None
        self.stream = ctypes.c_void_p()
        self.device_input = ctypes.c_void_p()
        self.device_output = ctypes.c_void_p()
        self.cuda: Any = None
        self.host_output: Any = None
        self.sequence = 0
        self.successes = 0
        self.failures = 0
        self.expired = 0
        self.anomalies = 0
        self.last_success_ns: int | None = None
        self.load_ms: float | None = None

    def _cuda_check(self, code: int, category: str) -> None:
        if code != 0:
            raise RuntimeFailure(category)

    def _configure_cuda(self) -> None:
        self.cuda = ctypes.CDLL(ctypes.util.find_library("cudart") or "libcudart.so.13")
        self.cuda.cudaSetDevice.argtypes = [ctypes.c_int]
        self.cuda.cudaStreamCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.cuda.cudaMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
        self.cuda.cudaMemcpyAsync.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self.cuda.cudaStreamSynchronize.argtypes = [ctypes.c_void_p]
        self.cuda.cudaFree.argtypes = [ctypes.c_void_p]
        self.cuda.cudaStreamDestroy.argtypes = [ctypes.c_void_p]
        self._cuda_check(self.cuda.cudaSetDevice(0), "cuda_device_error")

    def load(self, model_id: str) -> dict[str, Any]:
        if self.state == "READY" and self.model_id == model_id:
            return self.status()
        self.unload()
        self.state = "LOADING"
        started = time.perf_counter_ns()
        try:
            engine_path, _ = verify_engine(model_id)
            import tensorrt as trt  # type: ignore[import-not-found]

            if trt.__version__ != EXPECTED_TRT:
                raise RuntimeFailure("tensorrt_version_mismatch")
            self._configure_cuda()
            logger = trt.Logger(trt.Logger.ERROR)
            self.runtime = trt.Runtime(logger)
            self.engine = self.runtime.deserialize_cuda_engine(engine_path.read_bytes())
            if self.engine is None or self.engine.num_io_tensors != 2:
                raise RuntimeFailure("engine_deserialize_failed")
            contracts = {
                self.engine.get_tensor_name(index): (
                    tuple(self.engine.get_tensor_shape(self.engine.get_tensor_name(index))),
                    str(self.engine.get_tensor_dtype(self.engine.get_tensor_name(index))),
                )
                for index in range(self.engine.num_io_tensors)
            }
            if contracts != {
                INPUT_NAME: (INPUT_SHAPE, "DataType.FLOAT"),
                OUTPUT_NAME: (OUTPUT_SHAPE, "DataType.FLOAT"),
            }:
                raise RuntimeFailure("engine_tensor_contract_mismatch")
            self.context = self.engine.create_execution_context()
            if self.context is None:
                raise RuntimeFailure("context_create_failed")
            self._cuda_check(
                self.cuda.cudaStreamCreate(ctypes.byref(self.stream)), "cuda_stream_create_failed"
            )
            self._cuda_check(
                self.cuda.cudaMalloc(ctypes.byref(self.device_input), INPUT_BYTES),
                "cuda_input_allocate_failed",
            )
            self._cuda_check(
                self.cuda.cudaMalloc(ctypes.byref(self.device_output), OUTPUT_BYTES),
                "cuda_output_allocate_failed",
            )
            self.host_output = (ctypes.c_float * (YOLOX_ROWS * YOLOX_FEATURES))()
            if not self.context.set_tensor_address(INPUT_NAME, self.device_input.value):
                raise RuntimeFailure("input_address_failed")
            if not self.context.set_tensor_address(OUTPUT_NAME, self.device_output.value):
                raise RuntimeFailure("output_address_failed")
            self.model_id = model_id
            self.load_ms = (time.perf_counter_ns() - started) / 1e6
            self.state = "READY"
            return self.status()
        except Exception as exc:
            self.state = "FAILED"
            self.unload(failed=True)
            if isinstance(exc, RuntimeFailure):
                raise
            raise RuntimeFailure("model_load_failed") from None

    def infer(self, fd: int, metadata: dict[str, Any]) -> dict[str, Any]:
        if self.state != "READY":
            raise RuntimeFailure("model_not_ready")
        deadline = metadata.get("deadline_monotonic_ns")
        if deadline is not None and (
            not isinstance(deadline, int) or time.monotonic_ns() >= deadline
        ):
            self.expired += 1
            raise RuntimeFailure("request_expired")
        started = time.perf_counter_ns()
        try:
            libc = ctypes.CDLL(None)
            libc.mmap.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_long,
            ]
            libc.mmap.restype = ctypes.c_void_p
            libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            mapped_address = libc.mmap(None, INPUT_BYTES, 1, 1, fd, 0)
            if mapped_address == ctypes.c_void_p(-1).value:
                raise RuntimeFailure("tensor_map_failed")
            stage = time.perf_counter_ns()
            try:
                self._cuda_check(
                    self.cuda.cudaMemcpyAsync(
                        self.device_input, mapped_address, INPUT_BYTES, 1, self.stream
                    ),
                    "cuda_h2d_failed",
                )
                self._cuda_check(self.cuda.cudaStreamSynchronize(self.stream), "cuda_sync_failed")
            finally:
                libc.munmap(mapped_address, INPUT_BYTES)
            h2d_ms = (time.perf_counter_ns() - stage) / 1e6
            stage = time.perf_counter_ns()
            if not self.context.execute_async_v3(stream_handle=self.stream.value):
                raise RuntimeFailure("tensorrt_execute_failed")
            self._cuda_check(self.cuda.cudaStreamSynchronize(self.stream), "cuda_sync_failed")
            inference_ms = (time.perf_counter_ns() - stage) / 1e6
            stage = time.perf_counter_ns()
            self._cuda_check(
                self.cuda.cudaMemcpyAsync(
                    ctypes.cast(self.host_output, ctypes.c_void_p),
                    self.device_output,
                    OUTPUT_BYTES,
                    2,
                    self.stream,
                ),
                "cuda_d2h_failed",
            )
            self._cuda_check(self.cuda.cudaStreamSynchronize(self.stream), "cuda_sync_failed")
            d2h_ms = (time.perf_counter_ns() - stage) / 1e6
            stage = time.perf_counter_ns()
            detection_config = decoder_module.DetectionConfig(
                candidate_score_threshold=float(metadata["candidate_score_threshold"]),
                nms_iou_threshold=float(metadata["nms_iou_threshold"]),
            )
            detections, anomalies = decoder_module.person_detections(
                cast(Sequence[float], self.host_output), detection_config
            )
            postprocess_ms = (time.perf_counter_ns() - stage) / 1e6
            self.sequence += 1
            self.successes += 1
            self.anomalies += anomalies
            self.last_success_ns = time.monotonic_ns()
            output_bytes = memoryview(self.host_output).cast("B")
            result: dict[str, Any] = {
                "frame_id": metadata["frame_id"],
                "model_id": MODEL_ID,
                "model_artifact_sha256": ENGINE_SHA256,
                "inference_sequence": self.sequence,
                "detections": detections,
                "output_anomaly_count": anomalies,
                "timing": {
                    "queue_wait_ms": 0.0,
                    "h2d_ms": h2d_ms,
                    "inference_ms": inference_ms,
                    "d2h_ms": d2h_ms,
                    "postprocess_ms": postprocess_ms,
                    "worker_total_ms": (time.perf_counter_ns() - started) / 1e6,
                },
            }
            if metadata.get("qualification_candidates") is True:
                candidates, _ = decoder_module.decode_person_candidates(
                    cast(Sequence[float], self.host_output), detection_config
                )
                result["qualification_candidates"] = [
                    [item.x1, item.y1, item.x2, item.y2, item.score, item.source_index]
                    for item in candidates
                ]
            if metadata.get("qualification_digest") is True:
                result["raw_output_sha256"] = hashlib.sha256(output_bytes).hexdigest()
                floats = self.host_output
                result["raw_output_statistics"] = {
                    "minimum": min(floats),
                    "maximum": max(floats),
                    "finite_count": sum(1 for value in floats if math.isfinite(value)),
                    "sum": math.fsum(floats),
                    "sum_squares": math.fsum(value * value for value in floats),
                }
            return result
        except RuntimeFailure:
            self.failures += 1
            raise
        except Exception:
            self.failures += 1
            raise RuntimeFailure("inference_failed") from None

    def unload(self, *, failed: bool = False) -> dict[str, Any]:
        if self.cuda is not None:
            if self.stream.value:
                self.cuda.cudaStreamSynchronize(self.stream)
            if self.device_input.value:
                self.cuda.cudaFree(self.device_input)
            if self.device_output.value:
                self.cuda.cudaFree(self.device_output)
            if self.stream.value:
                self.cuda.cudaStreamDestroy(self.stream)
        self.context = self.engine = self.runtime = self.cuda = None
        self.host_output = None
        self.stream = ctypes.c_void_p()
        self.device_input = ctypes.c_void_p()
        self.device_output = ctypes.c_void_p()
        self.model_id = None
        self.state = "FAILED" if failed else "UNLOADED"
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "model_state": self.state,
            "model_id": self.model_id,
            "model_artifact_sha256": ENGINE_SHA256 if self.model_id else None,
            "load_ms": self.load_ms,
            "inference_successes_total": self.successes,
            "inference_failures_total": self.failures,
            "expired_frames_total": self.expired,
            "output_anomalies_total": self.anomalies,
            "last_successful_inference_monotonic_ns": self.last_success_ns,
        }
