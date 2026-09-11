# ADR 0012: ByteTrack-style temporal person tracking

## Status

Accepted provisionally for R4D recorded-video qualification.

## Decision

VeoTrex performs PERSON tracking in the edge process after the R4C detector has returned source-coordinate boxes. The TensorRT worker remains responsible only for bounded model execution. The tracker is a native NumPy/SciPy implementation derived from the ideas in FoundationVision's ByteTrack project, not a vendored ByteTrack package or a byte-for-byte port. ByteTrack is MIT licensed, Copyright 2021 Yifu Zhang; the consulted upstream revision was `d1bf0191adff59bc8fcfeaa0b33d3d1642552a99`. The associated paper is *ByteTrack: Multi-Object Tracking by Associating Every Detection Box* (ECCV 2022, arXiv:2110.06864). The Kalman module retains this provenance and license attribution.

The R4C evidence defines a fixed `TRACKING_HIGH_RECALL` detector profile with candidate score 0.05 and PERSON NMS IoU 0.45. The tracker partitions scores without a boundary gap: high is `score >= 0.30`, low is `0.05 <= score < 0.30`, and lower scores are discarded. Established tracks first associate with high-score detections and unmatched confirmed tracks then associate with low-score detections. Tentative tracks use remaining high detections. Only unmatched high detections at or above 0.30 may create tracks. Assignment uses SciPy 1.18.1 `linear_sum_assignment`, IoU cost (`1 - IoU`), explicit maximum-cost settings, and deterministic tie perturbation.

Motion uses an eight-dimensional float64 XYAH Kalman state: center, aspect ratio, height, and their velocities. Every update supplies a source timestamp; prediction scales velocity by actual elapsed time. Lost retention uses seconds. The provisional retention is 2.0 seconds, and a gap over 2.5 seconds causes an explicit discontinuity and clears stale tracks. Sequence regression, timestamp regression, malformed boxes, invalid numeric state, and resolution changes receive bounded deterministic handling.

Tracks have tentative, confirmed, lost, and removed states. Two observations are required for confirmation. State is isolated by `stream_instance_id`; IDs start at one in each new stream session and are only meaningful as the pair `(stream_instance_id, track_id)`. They are ephemeral trajectory labels, not a child, teacher, face, biometric, or cross-camera identity. Any future identity design requires a separate privacy and security review.

Bounds are 300 detections per frame, 128 active tracks, 128 retained lost tracks, 32 trajectory samples per track, and 16 stream instances. Metrics contain aggregate counters and timing only; track IDs, frame IDs, people, and arbitrary stream URLs are not metric labels.

## Qualification evidence

R4D used four public, rights-cleared Wikimedia Commons videos totaling 576.3 seconds. The principal path was GStreamer software VP8/VP9 decode to deterministic quality-95 JPEG frames, the R4C reference preprocessing path, the R4B TensorRT production worker, source-coordinate PERSON decoding, and this tracker. JPEG extraction is a lossy qualification transport and is not the future live-camera transport. Cached predictions include video and model hashes, preprocessing/profile/configuration metadata, timestamps, and their own content hash; caches and all media remain ignored.

The bounded cadence study selected 5 FPS. On the dense Boston clip it produced a lower extremely-short-track fraction than 8, 10, or 15 FPS while retaining more temporal evidence than the deliberate 2 FPS degradation boundary. Two-observation confirmation sharply reduced short confirmed trajectories compared with immediate confirmation. A 2.0-second lost window reduced track creation and short-track rate relative to 0.5 and 1.0 seconds. The provisional configuration is therefore scores 0.05/0.30/0.30, NMS 0.45, association costs 0.80/0.50/0.70, lost retention 2.0 seconds, discontinuity 2.5 seconds, and two-observation confirmation.

The synthetic and recorded evidence supports `KEEP_BYTETRACK_STYLE_PROVISIONAL`. Low-score association reduced misses and fragmentation in the controlled confidence-drop sequence. Tracking reduced raw count variation on all four clips, and tracker CPU time remained small relative to detector processing. Dense-crowd review still showed substantial track churn and detector-dependent misses, so these diagnostics are not identity accuracy or official MOT metrics.

## Dataset and deployment boundary

R4D intentionally did not use MOT17, MOT20, DanceTrack, SportsMOT, CrowdHuman, random YouTube media, scraped stock footage, or other restricted/non-commercial tracking media. Public adult-person clips validate software behavior, association, temporal stability, and resources. They cannot qualify daycare tracking accuracy.

Before any daycare decision, a rights-cleared, site-representative evaluation must cover children and adults, seated and crawling/floor activity, dense play and mutual occlusion, high corner viewpoints, doorways, indoor/outdoor lighting transitions, and furniture occlusion. R4D adds no camera transport, persistent identity, staff/child classification, occupancy rule, ratio rule, behavior classification, or alert.
