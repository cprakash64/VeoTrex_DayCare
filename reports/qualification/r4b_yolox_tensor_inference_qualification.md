# R4B YOLOX-S FP16 production tensor inference qualification

## Result

PASS. The isolated system-Python worker now provides trusted, bounded PERSON-only tensor inference using the exact R4A-selected YOLOX-S FP16 PLAN. This remains a preprocessed model-tensor API, not a camera detector.

## Artifact and trust

Logical model ID `yolox-s-fp16` resolves to the ignored local `yolox_s_fp16_a.plan`. The worker reuses the R4A manifest validator and then enforces the fixed identity and contract before deserialization.

- ONNX SHA-256: `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063`
- PLAN SHA-256: `f204dff3573a15647266ba287f789d266fd95a912dd0a75e973ab046e3991068`
- PLAN bytes: 21,356,188
- TensorRT: 10.16.2.10, CUDA 13.2, L4T 39.2.0, aarch64
- Input: `images`, FP32 `[1,3,640,640]`
- Output: `output`, FP32 `[1,8400,85]`

No IPC operation accepts a path. Tests cover valid local approval, missing artifacts, wrong hashes, symlinks, arbitrary logical IDs, model identity, tensor contracts, and compatibility mismatches.

## Worker and transport

Protocol v1 retains `HELLO`, `HEALTH`, and `SHUTDOWN` and adds `LOAD_MODEL`, `MODEL_STATUS`, `INFER_TENSOR`, and `UNLOAD_MODEL`. Input bytes never enter JSON. The parent creates an exact 4,915,200-byte sealed memfd and sends exactly one descriptor over the existing Unix `SOCK_SEQPACKET` socket. The worker checks regular-file type, size, write/grow/shrink seals, maps read-only, and closes the descriptor after use. Missing, extra, mutable, malformed, or incorrectly sized inputs fail with bounded categories.

One loaded model owns one TensorRT execution context, CUDA stream, pair of reusable device buffers, and reusable host output buffer. TensorRT 10 named tensor addresses and `execute_async_v3` are used. The parent permits one active inference and immediately rejects overlap as backpressure; no inference queue exists. Expired deadlines are rejected before descriptor creation and GPU execution.

## Decode and output

The external YOLOX raw-head decoder uses 80x80, 40x40, and 20x20 grids at strides 8, 16, and 32. It evaluates COCO person class 0 only and computes `objectness * person_probability`. Invalid finite state, exponential overflow, invalid area, and invalid score candidates are dropped and counted. Coordinates are clipped model-input coordinates from 0 through 640.

Provisional engineering defaults are candidate score 0.25, single-class NMS IoU 0.45, pre-NMS top 300, and final maximum 100. Ordering is descending score with source-row tie breaking. Output contains bounded `PERSON` detections and queue, H2D, TensorRT, D2H, postprocess, and worker-total timing.

## Unit and regression qualification

The focused worker/transport/trust/decoder/NMS/lifecycle suite passes 57/57. The database-independent repository suite passes 171/171 after adding R4B coverage. Strict mypy passes. R4B-touched Python paths pass Ruff and format checks. The repo-wide Ruff check continues to report the pre-existing S105 false positive on the Ring OAuth token URL, and repo-wide format check continues to identify the pre-existing `test_qualification_resources.py`; neither file was changed in R4B.

The edge `qualify-env` command passes. Its result confirms Jetson Orin Nano Super, aarch64, L4T R39.2.0, kernel 6.8.12-1021-tegra, GStreamer 1.24.2, and all protected elements: `nvv4l2decoder`, `nvvidconv`, `rtspsrc`, H.264/H.265 depayloaders and parsers. `uv.lock` is unchanged. No package operation or dependency change occurred.

## Real load and inference

Three consecutive production load/status/unload cycles passed, followed by additional fresh load/infer/unload checks. Load latencies were 247.741, 121.864, and 119.278 ms; later fresh loads were 216.977 and 228.626 ms. The model reached `READY` each time, and cleanup returned it to `UNLOADED`.

All-zero, constant 0.5, and deterministically seeded finite pseudo-random tensors ran through production memfd/SCM_RIGHTS IPC twice each. Every repeat produced the same detections and raw-output SHA-256. The three output digests were:

- zero: `937c025e9efb864438a4134110b107fd60cea5a457a139f330f12cde412d0a23`
- constant: `9e2f56d88fcfe88b1a6f0f545b10665edbb1bf74b66b05af0975f47152c38ee2`
- seeded: `0578fa192e3c60fc0fc381cb09933b153efa5f74e0723613ac7eaae28630e6b9`

Synthetic tensors produced zero person detections, as expected without a visual claim. There were no CUDA, TensorRT, or output-anomaly errors.

## Independent raw-output cross-check

`trtexec` independently loaded the same PLAN and exact zero tensor using `--loadInputs`, performed one inference, and exported its output. Both paths returned 714,000 finite values with minimum -2.21875. Maximum was 3.72656 in JSON and 3.7265625 in the worker's FP32 buffer. Relative aggregate differences caused by trtexec JSON decimal formatting were 1.053e-8 for the sum and 2.725e-8 for sum of squares. The production raw-buffer digest is exposed only when the explicit qualification flag is set.

## Performance

After 20 warmups, three repetitions of 500 sequential production RPC inferences completed. These timings cover the tensor RPC path and are not camera-to-event latency.

| Repetition | RPC mean / p50 / p95 / p99 ms | TRT mean / p95 ms | H2D mean | D2H mean | Post mean | Worker mean | FD before/after | RSS KiB before/after |
|---|---|---|---:|---:|---:|---:|---|---|
| 1 | 39.043 / 38.726 / 43.044 / 52.903 | 9.780 / 12.586 | 2.547 | 0.817 | 20.459 | 33.907 | 46 / 46 | 676696 / 676976 |
| 2 | 38.988 / 39.030 / 43.470 / 45.856 | 9.924 / 12.633 | 2.526 | 0.853 | 20.316 | 33.908 | 46 / 46 | 676976 / 657952 |
| 3 | 38.914 / 38.890 / 42.654 / 44.470 | 9.823 / 12.531 | 2.534 | 0.777 | 20.530 | 33.946 | 46 / 46 | 657952 / 657964 |

The execution means agree with the R4A 9.892 ms engine-only mean. RPC overhead is primarily bounded Python postprocessing, memfd creation/write, descriptor transfer, and synchronization. No FD or RSS growth remained after reusable host-buffer correction.

## Ten-minute inference soak

The final soak ran 600.129 seconds at approximately 5 requests/second and completed 2,998 actual inferences. Failures, expired frames, anomalies, protocol errors, backpressure events, and restarts were all zero. The model stayed `READY`; no reload or child process occurred. Worker RSS fell from 655,084 KiB to 568,492 KiB. FD count was 46 at steady samples, with one transient 47 and return to 46.

Across 119 tegrastats samples, system RAM usage was 5,567–6,105 MiB (mean 5,724 MiB), GR3D was 20–94% (mean 37.69%), and GPU temperature was 52.625–53.906 C (mean 53.310 C). No CUDA/TensorRT error, zombie, or unbounded resource growth occurred.

## Source safety and next gate

The PLAN, ONNX, tensor input, trtexec output, performance JSON, soak JSON, and tegrastats logs remain ignored or under `/tmp`. No model binary, tensor dump, customer data, imagery, recording, credential, private key, environment file, or camera URL is included in source. R4B is ready for the separately qualified image preprocessing and visual validation stage. Tracking, childcare classifications, occupancy, alerts, and camera ingestion remain out of scope.
