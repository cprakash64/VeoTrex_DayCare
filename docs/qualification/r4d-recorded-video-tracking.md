# R4D recorded-video temporal tracking qualification

## Outcome

`KEEP_BYTETRACK_STYLE_PROVISIONAL` with a provisional 5 FPS processing cadence. This report records engineering qualification of the tracker and does not claim daycare tracking accuracy or biometric identity.

## Production path

Four rights-cleared public adult-person videos were decoded with explicit GStreamer argv, sampled to high-quality JPEG frames, and replayed through decoded RGB, exact R4C YOLOX preprocessing, sealed-memfd IPC, the R4B TensorRT 10.16.2 worker, the fixed `TRACKING_HIGH_RECALL` profile, source-coordinate PERSON boxes, and the edge-process tracker. Principal production inference processed 2,886 frames covering 576.4 seconds with zero GPU-worker restarts. All downloaded media, frames, overlays, caches, and raw telemetry are ignored artifacts.

The selected tracker configuration is low/high/new scores 0.05/0.30/0.30, PERSON NMS 0.45, first/second/tentative maximum association costs 0.80/0.50/0.70, 2.0 seconds lost retention, 2.5 seconds discontinuity, and two observations to confirm. Low scores never create tracks. Capacity is bounded to 300 detections, 128 active and 128 lost tracks, 32 history samples, and 16 streams.

## Stability diagnostics

These are descriptive diagnostics without ground-truth identities. At 5 FPS, raw versus confirmed count standard deviation was 2.807/1.779 for Big City Life, 12.623/5.437 for Boston Crowd, 3.155/1.958 for Pedestrian Delivery, and 1.355/0.861 for Silk Road Walker. Corresponding extremely-short confirmed-track fractions were 0.087, 0.343, 0.259, and 0.071. Low-score recovery observations were 23, 107, 82, and 58. The dense Boston clip exposed material churn (467 observed confirmed trajectories and 266.35 tracks/minute under the initial 1.0-second inference-cache configuration); this is a known recorded-video limitation and not a passing identity-accuracy claim.

Tracker update mean/p99 latency in milliseconds was 3.468/6.885, 5.264/11.517, 1.676/3.607, and 1.038/2.361 respectively. Full sequential replay real-time factors were 1.19, 1.56, 1.54, and 1.55, including decode, CPU reference preprocessing, TensorRT, postprocessing, mapping, and tracking.

## Controlled evidence

In the deterministic 50-frame confidence-drop fixture, two-stage association matched 48 observations with 2 misses, 1 fragmentation, and 5 low-score recoveries. Removing the low-score stage matched 43 with 7 misses and 3 fragmentations. Both had zero ID switches in this fixture. Gap tests retained ID 1 through 2.0 seconds, removed it and created ID 2 at 2.1 seconds, and treated 3.0 seconds as a discontinuity. Three fresh cached replays produced the identical canonical SHA-256 `5134443262ccc7c8aa95219f70d17192f4c16b409fe9e3e699d222c7a4e2ffcd`.

On the Boston sequence, 5 FPS produced 466 trajectories with a 0.341 short-track fraction. The 8, 10, and 15 FPS trials produced 610/0.359, 700/0.404, and 728/0.382. The 2 FPS degradation boundary produced 268/0.377 but removed temporal evidence and delayed confirmation. Immediate confirmation produced 1,289 trajectories and a 0.593 short-track fraction versus 466/0.341 with two observations. The bounded lost-window study favored 2.0 seconds (394 trajectories, 0.300 short fraction) over 0.5 seconds (522, 0.404) and the 1.0-second baseline (466, 0.341).

SciPy 1.18.1 adds approximately 0.910 seconds of cold import startup and 55,716 KiB steady RSS in the isolated probe. It resolved as an aarch64 binary wheel against the unchanged NumPy 2.4.6 lock. This cost is accepted for mature deterministic assignment and must be included in edge-memory budgeting.

The complete-path soak ran for 902.21 seconds and processed 6,567 frames across 12 explicit stream resets. There were no inference or tracking failures and no GPU-worker restarts. Parent FDs stayed exactly 5; worker FDs stayed between 46 and 47. Parent RSS stayed between 73,208 and 92,952 KiB and worker RSS between 409,828 and 545,124 KiB. Active tracks peaked at 22, lost tracks at 37, and retained trajectory samples at 630, all below configured aggregate bounds. Mean processed-frame latency improved from 143.68 ms in the first tenth to 136.02 ms in the last tenth. Across 677 `tegrastats` samples, GPU utilization averaged 36.87% and peaked at 97%; GPU temperature stayed between 50.63°C and 53.34°C. System RAM ended at 6,034 MiB versus 5,721 MiB at capture start and stayed within 5,698–6,569 MiB.

## Visual and privacy review

Contact sheets were reviewed around high-activity, lost/recovered, crossing, entry, and exit frames for all four clips. They show coherent short-term IDs in ordinary motion and the expected detector/tracker churn in dense or distant groups. They also show missed people and occasional low-confidence/tentative boxes. No faces were cropped or stored separately, and overlays contain only ephemeral track ID, PERSON box, score, and lifecycle state.

Generic public recorded clips cannot qualify children, daycare viewpoints, dense play, floor activity, room transitions, or production camera transport. Those require rights-cleared site-representative validation before childcare semantics or decisions.
