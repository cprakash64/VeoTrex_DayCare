# Ring continuous-stream qualification

## Purpose and boundary

Stage 1D-A is an engineering measurement gate. It determines whether Ring Partner API live streams
are technically suitable for a proposed use; it is not the production video pipeline and passing it
is not regulatory certification or a declaration that VeoTrex is safe. It performs no inference,
tracking, alerting, recording, screenshotting, clipping, or classroom activation. By default it
retains only buffer metadata and sanitized resource measurements.

Use the harness only with facility authorization, after hours and in empty rooms where practical,
and avoid identifiable child footage. Never turn qualification into covert monitoring.

## Ring RTSPS contract

This design was checked against the official [Ring Partner API documentation](https://developer.amazon.com/docs/ring/api-documentation.html)
on 2026-08-24. The endpoint is
`rtsps://video.rtsp.amazonvision.com:322/v1/devices/{device_id}/stream`, with an optional encoded
`component_id` query value. Signaling and interleaved RTP use the same TLS/TCP connection; UDP and
plain RTSP are not supported. TLS verification remains enabled.

Ring authenticates `DESCRIBE`. The GStreamer backend sets the access token in process memory on the
`rtspsrc.user-pw` connection property. It never puts a token in the URL, argv, environment, report,
or log and never starts `gst-launch`. Do not enable verbose GStreamer diagnostics in a real test:
third-party debug or crash tooling may serialize authentication properties.

The SDP/negotiated RTP caps—not inventory capabilities—select the media path:

- H.264: `rtspsrc -> rtph264depay -> h264parse`
- H.265/HEVC: `rtspsrc -> rtph265depay -> h265parse`
- optional audio: Opus or PCMU, continuity metadata only

Transport mode terminates compressed video in a `fakesink` and measures buffers, bytes, timestamps,
bitrate, stalls, EOS, and failures. Decode mode adds a negotiated decoder and measures decoded-frame
latency/count, resolution, FPS, arrival gaps, and PTS/DTS anomalies. On Jetson, `auto` requires
`nvv4l2decoder`; it does not silently fall back to software. Non-Jetson development can select a
reported software decoder.

## Session lifecycle and continuity

The operator explicitly selects `battery_30_seconds` or `line_powered_60_seconds`. These are Ring's
current documented maximum classes, not assumptions about the exact disconnect instant. Every
session records the configured class and actual observed monotonic duration. The harness records
request, connect, SDP/DESCRIBE when observable, PLAY, first/last compressed buffer, first/last decoded
frame, termination, and teardown timestamps.

Sequential qualification deliberately opens a new pipeline for every provider session. Before each
open it calls the injected token provider. A deployed integration must inject Stage 1B's
`RingLinkService.get_valid_access_token(tenant_id, connection_id)`; that service serializes refresh,
rotates both tokens once, and fails closed for `REFRESH_UNCERTAIN`, `REAUTH_REQUIRED`, `DISCONNECTED`,
and removed connections. The harness contains no refresh-token mechanism. The manual CLI path is
labelled development-only and keeps a no-echo token only in memory.

For adjacent usable sessions, transition gap is:

```text
gap_n = first_media_(n+1) - last_media_n
```

Negative gaps are retained as overlap. Let `W` be the monotonic interval from the first usable media
of the first session to the last usable media of the final session, and
`B = sum(max(0, gap_n))`. The report uses:

```text
media_availability_percent = max(0, W - B) / W * 100
```

when `W > 0`; otherwise availability is zero and the decision is `INSUFFICIENT_DATA`. A connected
TCP session with no useful media is unavailable. Reported continuity includes min/p50/p95/p99/max
transition gaps, total blind time, largest in-session stall, repeated/regressing PTS/DTS, and bounded
frame-gap samples. Statistics storage is bounded for multi-hour runs.

## Experiments

Sequential tests distinguish provider expiry, 401/token failure, TCP disconnect, TLS failure, camera
offline, no-frame stall, codec negotiation failure, decoder failure, cancellation, and application
failure. Generic retry is restricted to selected transient transport categories and is bounded.

Overlap is optional and never production behavior. At `expected_limit - lead_seconds`, session B is
attempted while A remains active. The report records whether B was accepted, first B/last A media,
negative overlap or positive gap, whether A was disrupted, and provider rejection. The harness does
not work around a concurrent-session restriction.

Multi-camera tests accept only explicit concurrency 1, 2, 4, 8, or 12, require exactly that many
VeoTrex Camera UUIDs, and ramp starts. They never automatically open twelve streams. Reports retain
per-camera values plus shared system CPU, RSS/RAM, load, NIC counters, disk, and optional Jetson
metrics. No arbitrary Internet speed test or upload is performed.

## Engineering rubric

Targets are configurable and the chosen values are serialized. Defaults are engineering targets,
not Ring guarantees or legal rules:

- critical safety-video suitability: availability at least 99.9%, p99 transition gap at most 500 ms,
  and no recurrent deterministic blind interval over 1 second;
- non-critical occupancy/operations suitability: availability at least 99.5% and p99 transition gap
  at most 2 seconds.

Outputs are `MEETS_STREAM_TARGET`, `CONDITIONAL`, `DOES_NOT_MEET_STREAM_TARGET`, or
`INSUFFICIENT_DATA`. Memory growth, thermal throttling that causes loss, credential leakage, or
unstable expiry recovery must still fail review. A 1–2 second gap might be tolerable for a persistent
10-minute phone-use condition, occupancy trend, or ratio trend; it may miss a fall, bite, collision,
or rapid exit. Do not convert one result into a binary product-safety judgment.

## Manual Stage 1D-B runbook (do not run during implementation)

First perform read-only inspection:

```bash
uv run --package veotrex-edge-agent veotrex-edge qualify-env
```

Then, on an authorized host, use a VeoTrex Camera UUID. Provider identity and the token are prompted
without echo; neither is accepted as a normal command-line argument:

```bash
uv run --package veotrex-edge-agent veotrex-edge qualify-ring \
  --camera-id 00000000-0000-0000-0000-000000000001 \
  --mode transport --session-class line_powered_60_seconds \
  --cycles 10 --manual-prompt

uv run --package veotrex-edge-agent veotrex-edge qualify-ring \
  --camera-id 00000000-0000-0000-0000-000000000001 \
  --mode decode --session-class line_powered_60_seconds \
  --cycles 10 --decoder nvidia --manual-prompt

uv run --package veotrex-edge-agent veotrex-edge qualify-ring \
  --camera-id 00000000-0000-0000-0000-000000000001 \
  --mode transport --session-class line_powered_60_seconds \
  --cycles 2 --overlap-lead 5 --manual-prompt
```

For concurrency, repeat `--camera-id` exactly 2, 4, 8, or 12 times and provide the matching explicit
`--concurrency` plus a controlled `--ramp-seconds`. Start at one and increase only after review. For a
fixed-duration soak, calculate cycles from the provider class (for example, approximately ten
60-second cycles for a ten-minute smoke test); runs may use thousands of bounded cycles.

Reports are written atomically under ignored `reports/qualification/`. SIGINT/SIGTERM stops new
sessions, tears down active pipelines, and marks the partial report incomplete. Never commit real
reports. A sanitized fake sample is in `docs/qualification/samples/`.

## Troubleshooting

- `GStreamer/PyGObject is unavailable`: install the OS GStreamer/PyGObject packages appropriate to
  the qualification host; they are intentionally not API dependencies.
- TLS failure: confirm `rtsps://`, port 322, system trust, clock synchronization, and a GLib TLS
  backend such as `glib-networking`.
- 461/transport failure: only interleaved RTP over TCP is supported.
- codec failure: inspect negotiated caps and the H.264/H.265 depay/parser plugins; do not hardcode
  inventory codec metadata.
- Jetson decoder failure: install/repair the NVIDIA-supported media stack outside this tool. The
  harness will not change drivers, JetPack, power mode, or fall back silently.
- 401: use the Stage 1B lifecycle; never append the token to a URL or retry an uncertain refresh.
- connected but unavailable: inspect no-frame stalls, timestamp behavior, and transport/decode mode
  differences.

DeepStream, inference, recording, tracking, alerts, and childcare safety behavior remain out of
scope until a separately reviewed later stage.
