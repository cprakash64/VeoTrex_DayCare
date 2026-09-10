# ADR 0009: Detector candidate qualification

## Decision

Evaluate exactly YOLOX-S at 640×640, YOLOX-Tiny at 416×416, and RT-DETRv2-R18-DSP at 640×640 for the first person-detector foundation. Selection remains provisional until R4B measures daycare-relevant small-person, partial-occlusion, and false-negative behavior. R4A stores model binaries and locally built TensorRT engines only under the ignored `artifacts/models/candidates/` tree. Committed code may validate versioned manifests, trusted paths, hashes, and platform compatibility, but it does not load or run production inference.

YOLOX official code is Apache-2.0 and its upstream documentation links official ONNX release assets. Upstream does not state a separate license for pretrained artifacts, so engineering evaluation may proceed while redistribution remains subject to license review. PaddleDetection code is Apache-2.0 and publishes the RT-DETRv2-R18-DSP COCO checkpoint and export instructions, but no official pre-exported ONNX was found. Its ONNX must be exported off-device from the official checkpoint using the documented PaddleDetection and Paddle2ONNX path before benchmarking.

## Ultralytics exclusion

R4A does not download or integrate Ultralytics software or pretrained weights, including YOLOv8, YOLO11, or YOLO26. Their current AGPL/commercial licensing choices do not match this stage's preference for permissively licensed candidates in a commercial edge product. A later explicit licensing decision may reconsider them.

## Qualification boundary

All PLAN files are built locally on the target Jetson with TensorRT 10.16.2. FP32 and FP16 are evaluated; INT8 waits for a qualified calibration and accuracy dataset. Performance figures measure engine execution only and are not camera-to-event latency. No model is integrated into the production GPU worker, and R4A adds no camera, tracking, classification, evidence, or daycare-event behavior.
