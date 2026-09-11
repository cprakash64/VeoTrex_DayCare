# R4C reference image and COCO person qualification

Date: 2026-09-10 (America/Phoenix)

Result: PASS

Model decision: `KEEP_YOLOX_S_PROVISIONAL`

## Source and dependencies

The run started from `main` at `1aba7abec245d60a5d61330a7f10c7bc33d58e3d`, equal to `origin/main`, with a clean tracked tree. R4C adds exactly NumPy 2.4.6 as the edge runtime array dependency and Pillow 12.3.0 in the qualification group. The lock contains CPython 3.12 Linux aarch64 wheels and no unrelated version change or source build.

The preprocessing contract follows current official non-legacy YOLOX behavior: explicit RGB8/BGR8 input, explicit RGB-to-BGR conversion, minimum-ratio aspect preservation, Python `int()` truncation, bilinear resize, top-left placement on a 114-filled canvas, BGR HWC-to-CHW conversion, contiguous FP32 `[1,3,640,640]`, and no division or normalization. Frozen transform metadata drives clipped floating-point source-coordinate inversion.

## Dataset provenance

Only official COCO endpoints were used. The HTTPS endpoint failed certificate name verification before transfer, so retrieval used the same official `images.cocodataset.org` origin over HTTP without disabling TLS checks.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `val2017.zip` | 815,585,330 | `4f7e2ccb2866ec5041993c9cf2a952bbed69647b115d0f74da7ce8f4bef82f05` |
| `annotations_trainval2017.zip` | 252,907,541 | `113a836d90195ee1f884e704da6304dfaaecff1f023f49b6ca93c4aaae470268` |
| `instances_val2017.json` | — | `e8c7f7908f1d7278341fae127d0da654f102f11bd7b21d8aeefa635b8c810b6f` |

Both archives passed complete ZIP integrity checks. Extraction produced all 5,000 validation JPEGs and the official annotation file with 5,000 images, 36,781 annotations, and 80 categories. Retrieval completed at 2026-09-10 16:44:42 -0700. Dataset, archives, predictions, and rendered galleries remain ignored.

## Independent reference

The independent Pillow translation matched scale and resized dimensions for every synthetic and 20 real-image comparison. Across the 20 real images, the maximum tensor difference was 1 intensity unit and the mean of per-image mean absolute differences was 0.0268. All 20 images had identical final detection counts. The maximum observed corresponding model-box shift was 2.49 pixels and score shift 0.0136; no material disagreement was observed.

## Person evaluation

The primary path processed all 5,000 images through safe decode, R4C preprocessing, sealed memfd/SCM_RIGHTS transport, the existing TensorRT worker, YOLOX PERSON decode/NMS, and source mapping. It included 2,307 no-person images and 10,777 non-crowd person ground truths. Score-ordered one-to-one matching was evaluated at IoU 0.50 and 0.75. Predictions substantially overlapping person crowd regions were ignored. These simplified metrics are not official COCO AP.

At IoU 0.50 with NMS 0.45:

| Score | Precision | Recall | F1 | FP/image | FN/image |
|---:|---:|---:|---:|---:|---:|
| 0.05 | 0.6080 | 0.8243 | 0.6998 | 1.1456 | 0.3788 |
| 0.10 | 0.7129 | 0.8022 | 0.7549 | 0.6962 | 0.4264 |
| 0.15 | 0.7711 | 0.7821 | 0.7766 | 0.5004 | 0.4696 |
| 0.20 | 0.8121 | 0.7628 | 0.7867 | 0.3804 | 0.5112 |
| 0.25 | 0.8434 | 0.7467 | 0.7921 | 0.2988 | 0.5460 |
| 0.30 | 0.8660 | 0.7316 | 0.7931 | 0.2440 | 0.5786 |
| 0.40 | 0.8995 | 0.6941 | 0.7835 | 0.1672 | 0.6594 |
| 0.50 | 0.9291 | 0.6529 | 0.7669 | 0.1074 | 0.7482 |

The tracking-stage candidates are 0.05 for high recall and 0.30 for balanced detection. Neither is an alert threshold, and the R4B production default is unchanged.

At score 0.30, IoU 0.50 produced TP 7,884, FP 1,220, FN 2,893, ignored 1,342, precision 0.8660, recall 0.7316, and F1 0.7931. IoU 0.75 produced TP 6,280, FP 2,468, FN 4,497, ignored 1,698, precision 0.7179, recall 0.5827, and F1 0.6433.

At score 0.30 and IoU 0.50, size recall was small 0.4782 of 4,308, medium 0.8746 of 3,723, and large 0.9352 of 2,746. Density recall was 0.9196 for one-person images, 0.7817 for 2–4, 0.7063 for 5–9, and 0.6815 for 10+. No-person images had 0.0459 false positives/image. Small and dense people remain the principal risks.

At score 0.10, raising NMS IoU from 0.45 to 0.50/0.60 increased overall recall from 0.8022 to 0.8063/0.8115 and 10+ recall from 0.7710 to 0.7743/0.7796, while false positives rose from 3,481 to 3,725/4,353. NMS 0.45 remains the candidate because the recall gain does not offset duplicate errors.

## Visual and performance review

Ignored contact sheets cover the ten highest-FN images, ten highest-FP images, small/distant people, dense/occluded groups, and representative geometry successes. Green ground truth and red scored predictions make missed distant people, dense overlap, edge truncation, and person-like object false positives visible.

Over all 5,000 images, timing in milliseconds was:

| Stage | Mean | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| encoded decode | 9.224 | 8.869 | 13.945 | 16.753 |
| color conversion | 0.012 | 0.011 | 0.014 | 0.016 |
| resize | 17.459 | 3.524 | 86.785 | 124.229 |
| full preprocess | 19.595 | 5.650 | 90.173 | 127.222 |
| memfd prepare | 3.835 | 3.710 | 4.807 | 5.406 |
| IPC round trip | 60.892 | 59.909 | 72.351 | 84.725 |
| H2D | 2.599 | 2.499 | 3.304 | 4.071 |
| TensorRT | 10.521 | 10.614 | 12.706 | 14.120 |
| D2H | 0.849 | 0.801 | 1.057 | 1.985 |
| YOLOX postprocess | 23.899 | 23.137 | 30.906 | 41.153 |
| source mapping | 0.073 | 0.058 | 0.244 | 0.455 |
| encoded image to result | 95.684 | 82.556 | 171.497 | 210.425 |

The dominant measured optimization targets are CPU resize tails and worker postprocessing/IPC serialization. R4A engine-only and R4B tensor-RPC baselines were approximately 9.8 ms and 39 ms mean; R4C's qualification-candidate export deliberately adds CPU decode and serialization overhead. Encoded-file latency does not represent the future decoded camera path.

The 5,000-image telemetry run used one worker with zero restarts across 549 samples: mean GR3D 40.4%, maximum 82%, and maximum observed temperature 58.3°C.

## Stability and regressions

The 600.3-second real-image soak completed 7,391 full-path images with zero failures and zero worker restarts. Parent FDs remained exactly 5; worker FDs stabilized at 46 (one startup sample at 47). Parent RSS ranged 47,620–59,336 KiB, worker RSS 569,504–657,416 KiB, and combined RSS 625,612–716,344 KiB as CUDA/runtime caches warmed and plateaued. Available system memory remained bounded at 728,452–1,503,912 KiB. Mean latency was 79.91 ms; first-tenth mean 81.53 ms and last-tenth mean 77.55 ms. Across 608 telemetry samples, mean GR3D was 40.2%, maximum 98%, and maximum temperature 57.9°C.

All 197 database-independent tests passed, including preprocessing, geometry, decoder robustness, evaluator, R4B/R3D worker, and inference tests. Ruff and strict mypy passed. The edge qualification CLI passed.

Platform invariants passed: L4T 39.2.0, kernel 6.8.12-1021-tegra, CUDA runtime package 13.2.75-1, TensorRT 10.16.2.10, GStreamer 1.24.2, RTSP/H.264/H.265 elements, `nvv4l2decoder`, `nvvidconv`, and 25W power mode. No APT operation occurred.

COCO does not qualify daycare alerts. Recorded-video work must evaluate children and adults at actual camera scale, seated/standing/crawling poses, partial occlusion, dense groups, doorways, floor activity, lighting, and mounting angles. No customer footage was used.
