# ADR 0010: trusted YOLOX tensor inference in the isolated GPU worker

## Status

Accepted provisionally for R4B.

## Decision

The R3D `/usr/bin/python3 -I` worker owns the provisional YOLOX-S FP16 TensorRT engine selected in R4A. The only accepted logical model ID is `yolox-s-fp16`; IPC cannot supply a path. The worker resolves the fixed local candidate, validates its qualified manifest, full SHA-256, byte size, parent ONNX digest, platform compatibility, model identity, precision, and exact FP32 tensor contracts before deserialization. The PLAN was built locally from the approved official YOLOX ONNX. It remains an ignored runtime artifact and is never committed.

The parent transfers only a preprocessed FP32 NCHW `[1,3,640,640]` tensor. It writes exactly 4,915,200 bytes into a sealed `memfd`, passes exactly one descriptor with `SCM_RIGHTS`, and sends bounded metadata through the existing `SOCK_SEQPACKET` protocol. The worker verifies the regular file, size, and write/grow/shrink seals, maps it read-only, and closes it after each request. No image, filesystem tensor, or JSON tensor path exists.

One model load creates one TensorRT 10 execution context, one CUDA stream, two reusable device buffers, and one reusable host output buffer. Named tensor addresses and `execute_async_v3` are required. The parent protects this context with a nonblocking single inference lane; overlap receives deterministic backpressure rather than entering a queue. Deadlines are checked before GPU work.

The official standard YOLOX output is decoded externally from `[1,8400,85]` using 80x80, 40x40, and 20x20 grids at strides 8, 16, and 32. R4B evaluates COCO class 0 only, with `objectness * person_probability`. It clips boxes to 0..640, rejects invalid numerical results, applies deterministic bounded single-class NMS, and returns `PERSON` detections in model-input coordinates. The candidate threshold (0.25), IoU threshold (0.45), pre-NMS limit (300), and output limit (100) are engineering defaults that require later daycare-relevant calibration.

FP16 is the provisional primary because the R4A-qualified local engine met the platform and throughput gate. R4B timings describe the complete tensor RPC and do not describe camera-to-event latency. JPEG decoding, resize and letterbox transforms, source-coordinate mapping, cameras, tracking, and childcare semantics remain for later stages. The next stage must qualify preprocessing and visual behavior before this can be described as a camera detector.
