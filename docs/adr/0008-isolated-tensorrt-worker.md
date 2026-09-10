# ADR 0008: Isolated TensorRT worker

## Decision

Keep JetPack TensorRT/CUDA libraries under APT and keep the edge-agent virtual environment isolated. The edge agent launches a dedicated unprivileged worker as `/usr/bin/python3 -I` with an argument vector, a minimal environment, and one inherited `AF_UNIX/SOCK_SEQPACKET` descriptor. There is no listener or filesystem socket. Protocol v1 is bounded deterministic JSON and supports only `HELLO`, `HEALTH`, and `SHUTDOWN`.

The supervisor reports `STOPPED`, `STARTING`, `READY`, `DEGRADED`, or `FAILED`. Readiness requires a correlated handshake and exact Python 3.12, aarch64, TensorRT 10.16.2, CUDA Runtime 13.2, and at least one CUDA device. Calls have timeouts. Restarts use bounded exponential backoff and open a circuit after the configured limit. Metrics use fixed names and no request/error labels.

## Engine trust boundary

TensorRT PLAN files are trusted executable deployment artifacts. A future loader must accept files only from a configured VeoTrex-controlled engine store, resolve and validate paths without traversal or symlink escape, verify SHA-256 against an approved manifest, verify TensorRT/platform metadata, and reject arbitrary uploads or customer-provided plans.

## Concurrency contract

Future code may share TensorRT engine objects only where TensorRT documents that behavior as safe. An `IExecutionContext` must never be used concurrently. Parallel inference must own separate execution contexts and CUDA streams as appropriate. TensorRT 10.x inference will use named tensors through `set_tensor_address(...)` and `execute_async_v3(...)`.

## Operations and scope

The production `veotrex-edge-agent` entry point constructs the supervisor. For a direct smoke check from `services/edge-agent`, run `.venv/bin/python -c 'from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor; s=GpuWorkerSupervisor(); print(s.start()); print(s.health()); s.stop()'`. The child alone imports apt TensorRT. State changes are structured log events, and `status()` exposes bounded worker state and fixed-name metrics.

R3D implements control-plane lifecycle, health, version gates, restart limits, and observability. It does not load ONNX/PLAN files, build production engines, run inference, ingest cameras, or implement vision/daycare behavior.
