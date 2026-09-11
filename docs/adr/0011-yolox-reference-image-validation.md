# ADR 0011: YOLOX reference image preprocessing and public validation

## Status

Accepted for R4C reference qualification.

## Decision

The correctness-reference decoded-frame input is an 8-bit, three-channel NumPy array with an explicit `RGB8` or `BGR8` pixel format. RGB is explicitly converted to BGR. The transform computes `scale = min(640/source_height, 640/source_width)` and uses Python `int()` truncation for resized dimensions. Bilinear resize uses the same half-pixel coordinate placement as OpenCV `INTER_LINEAR`. The resized image is placed at the top left of a BGR uint8 640x640 canvas filled with 114, transposed HWC to CHW, converted to contiguous FP32, and batched to `[1,3,640,640]`.

No division by 255, mean subtraction, standard-deviation division, centered padding, aspect-ratio stretching, or implicit channel guess is permitted. This matches the current official YOLOX non-legacy preprocessing branch. The historical release-commit ONNX demo used the legacy normalized helper; R4C deliberately follows the current non-legacy contract required for the selected deployment artifact and validates behavior on labeled images.

Every operation returns immutable transform metadata. Source coordinates divide model coordinates by the recorded scale and clip to source bounds. Existing model-space boxes remain in the output alongside source-space boxes. Coordinates stay floating point.

Pillow is confined to qualification image-file decoding and an independent interpolation comparison. The decoder bounds encoded bytes, dimensions, and decoded pixels; verifies content rather than extensions; rejects malformed/truncated/unsupported images; converts grayscale deterministically to RGB; and composites RGBA deterministically over black. Production camera ingestion will supply decoded frames and is outside R4C.

The reference CPU path establishes correctness rather than multi-camera performance. A future GStreamer/NVIDIA scaling or zero-copy path must be compared against this transform and detection behavior before adoption.

## Public evaluation

R4C uses all 5,000 official COCO 2017 validation images and official instance annotations, including person-negative images. Category 1 non-crowd boxes are ordinary ground truth. Predictions substantially covered by person `iscrowd` regions are ignored. Matching is deterministic, score ordered, and one-to-one at IoU 0.50 and 0.75. This simplified evaluator reports precision, recall, F1, errors per image, size recall, density behavior, score sweeps, and bounded NMS sensitivity; these values are not official COCO AP.

The score sweep selects candidates for later temporal evaluation rather than an alert threshold. COCO cannot qualify daycare decisions. Recorded-video stages must cover actual room viewpoints, children and adults at camera scale, floor activity, crawling/seated/standing poses, partial occlusion, dense groups, doorways, mounting angles, and lighting before automatic childcare semantics are considered.

The complete 5,000-image run selected 0.05 as the high-recall tracking candidate and 0.30 as the balanced detection candidate, with NMS remaining at 0.45. At score 0.30 and IoU 0.50, precision was 0.8660, recall 0.7316, and F1 0.7931. Recall was 0.4782 for small, 0.8746 for medium, and 0.9352 for large people. The materially lower small-person and dense-scene recall is a required input to recorded-video evaluation; it does not justify an alert threshold or final daycare claim.

The public-image evidence supports `KEEP_YOLOX_S_PROVISIONAL`. The pipeline geometry is correct, detector behavior is credible, and the sequential workload is stable, while COCO failure galleries show the expected strategic weakness for distant and crowded people. The 0.05 and 0.30 confidence values are tracking-stage candidates only. Production R4B defaults remain unchanged.
