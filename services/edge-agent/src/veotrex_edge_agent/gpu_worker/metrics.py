from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class GpuWorkerMetrics:
    """Bounded low-cardinality worker telemetry."""

    restarts_total: int = 0
    handshake_failures_total: int = 0
    health_failures_total: int = 0
    protocol_errors_total: int = 0
    inference_requests_total: int = 0
    inference_successes_total: int = 0
    inference_failures_total: int = 0
    expired_frames_total: int = 0
    backpressure_drops_total: int = 0
    parent_rpc_roundtrip_ms: float | None = None
    last_successful_health_monotonic: float | None = None

    def snapshot(self, *, up: bool, details: dict[str, Any] | None) -> dict[str, Any]:
        safe = details or {}
        return {
            "veotrex_gpu_worker_up": int(up),
            "veotrex_gpu_worker_restarts_total": self.restarts_total,
            "veotrex_gpu_worker_handshake_failures_total": self.handshake_failures_total,
            "veotrex_gpu_worker_health_failures_total": self.health_failures_total,
            "veotrex_gpu_worker_protocol_errors_total": self.protocol_errors_total,
            "veotrex_gpu_model_loaded": int(safe.get("model_state") == "READY"),
            "veotrex_gpu_inference_requests_total": self.inference_requests_total,
            "veotrex_gpu_inference_successes_total": self.inference_successes_total,
            "veotrex_gpu_inference_failures_total": self.inference_failures_total,
            "veotrex_gpu_expired_frames_total": self.expired_frames_total,
            "veotrex_gpu_backpressure_drops_total": self.backpressure_drops_total,
            "veotrex_gpu_parent_rpc_roundtrip_ms": self.parent_rpc_roundtrip_ms,
            "veotrex_gpu_output_anomalies_total": safe.get("output_anomalies_total", 0),
            "last_successful_health_monotonic": self.last_successful_health_monotonic,
            "tensorrt_version": safe.get("tensorrt_version"),
            "cuda_runtime_version": safe.get("cuda_runtime_version"),
            "cuda_device_count": safe.get("cuda_device_count"),
        }

    def counters(self) -> dict[str, Any]:
        return asdict(self)
