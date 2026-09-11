# R5A live camera transport qualification

Sanitized engineering record. Raw JSON reports remain under ignored `reports/qualification/`.
No customer media, frames, screenshots, credentials, account identifiers, private addresses, or
provider device identifiers were produced or retained. All media was synthetic `videotestsrc`
content generated in memory and served on loopback only.

## Boundary

R5A qualifies transport and hardware decode only. Decoded buffers terminate in `fakesink`; nothing
reaches YOLOX or the tracker. Platform, CUDA, TensorRT, GStreamer, NVIDIA multimedia packages,
Docker, and power mode were not modified. No package operation occurred.

## Method

`veotrex-edge qualify-transport --scenario <name>` runs the production path (controller, runner,
isolated `/usr/bin/python3 -I` GStreamer worker, `nvv4l2decoder`) against
`qualification/rtsp_fixture_server.py`: a loopback RTSP server that streams RTP interleaved over
TCP (the mode Ring documents for RTSPS), requires Basic authentication with a synthetic password
passed over stdin, supports fault injection (stall, drop, expiry, redirect, cross-origin control
URL), and runs a canary listener that counts foreign connections and credential presentation.
Fixture encode is software x264/x265 because this Orin Nano has no NVENC. The host desktop session
(browser, IDE) was running throughout, so system CPU and GR3D figures include unrelated load.

## Results

All runs: RTSP over TCP (interleaved RTP), Basic authentication, `nvv4l2decoder` with
`memory:NVMM` output, no FD, child-process, or zombie leak after stop (edge-agent FDs returned to
baseline every run), and no fixture password in any worker `/proc/<pid>/cmdline` or
`/proc/<pid>/environ` sample.

| Scenario | Outcome |
|---|---|
| H.264 1080p15, 60 s | 896 decoded buffers at 15.09 FPS; first decoded buffer 0.63 s after request (SDP 0.27 s, first compressed 0.60 s); gaps p50 66.7, p95 70.5, p99 112.5, max 115.5 ms; 0 duplicate, regressing, or missing PTS |
| H.265 1080p15, 60 s | 615/615 compressed buffers decoded; 10.4 FPS limited by the fixture's software x265 encoder, not decode; first decoded 0.88 s |
| H.265 720p15, 60 s | 895/895 decoded at 15.12 FPS; first decoded 0.73 s; gaps p50 64.9, p95 87.8, p99 186.5, max 296.5 ms (encoder jitter); 0 PTS anomalies |
| 3 controlled reconnects (SIGKILL of VeoTrex's own worker only) | Each killed worker reaped; recovery to `STREAMING` in 1.61, 2.61, 4.22 s (backoff 1/2/4 s because the budget resets only after 60 s of stable streaming); generations 1→4; new-generation first decoded buffer 0.55-0.57 s; 0 stale buffers |
| Stall 4 s | `DEGRADED` after 2.0 s of no media, back to `STREAMING` without reconnect |
| Stall 14 s | `DEGRADED` at 2 s, `MEDIA_STALLED` at 8 s, one reconnect; media resumed when the source did; no reconnect loop |
| Overlap renewal (30 s provider sessions, renew 5 s early), 160 s | 6 renewals, 0 renewal failures, 0 reconnects, at most 2 concurrent workers; handoff continuity gaps 60-199 ms (bounded by the worker's 200 ms reporting batch); 28 old-generation buffers rejected; 6 stream-instance discontinuities marked |
| Wrong credential | `AUTHORIZATION_FAILED` at 0.26 s, terminal `FAILED`, 0 reconnects |
| Connection refused | 4 attempts with 0.5/1.0/2.0 s backoff, circuit open (`FAILED`) at 4.4 s |
| RTSP 302 redirect | `REDIRECT_REFUSED` at 0.21 s; canary received 0 requests and 0 credentials (one blind TCP connect, see limitations) |
| Cross-origin SDP control URL | `REDIRECT_REFUSED`; canary received no connection and no credential |
| Provider drops the connection | `END_OF_STREAM`, one reconnect, `STREAMING` again 1.6 s later |

One-stream resources (1080p H.264): worker RSS about 86 MB, worker CPU about 5% of one core,
worker FDs 36-38, edge-agent RSS about 34 MB. NVDEC devfreq clock rose from 115 MHz idle to
128-166 MHz while decoding; NVDEC utilization itself requires debugfs and was not read. The
TensorRT GPU worker was not running (no detector workload in R5A). No thermal throttling was
observed (Tj about 51-53 °C).

Ring gate: no Ring environment variables, `.env` files, partner client credentials, linked account,
or managed vault adapter are configured; `RingLiveSessionProvider` fails closed with
`PROVIDER_NOT_CONFIGURED`. Sub-gate `BLOCKED_RING_ACCOUNT_NOT_AVAILABLE`.

## 30-minute soak

One camera, H.264 1080p15, 1801 s wall time, one session held throughout (`CONNECTING` at 0.0 s,
`STREAMING` at 0.65 s, `STOPPED` on request at 1800.1 s).

- 26 993 compressed buffers in, 26 993 decoded out, 15.003 FPS sustained, 26.8 MB of compressed
  media, all through `nvv4l2decoder` with NVMM output at 1920x1080.
- Inter-buffer gaps: p50 66.7 ms, p95 70.2 ms, p99 110.7 ms, max 125.6 ms.
- 0 duplicate, regressing, or missing PTS/DTS; 0 large reversals; 0 discontinuities; 0 stale
  generation buffers; 0 arrival-time regressions.
- 0 stalls, 0 reconnects, 0 renewals, 0 decoder errors, 0 authorization errors, 0 GStreamer
  protocol errors, 0 dropped worker samples; no unexplained media cessation.
- Worker RSS 85.94 MB to 86.20 MB (252 KB growth over 30 minutes), FDs 36-38, threads 16-17,
  CPU about 4.7% of one core. Edge-agent RSS 33.3 MB to 35.3 MB; that 2 MB is the harness holding
  164 resource samples in memory for the report and is bounded by run length, not a transport leak
  (its FDs stayed at 9-10 and returned to baseline).
- 0 zombie processes, 0 leaked child processes, worker exited cleanly (rc 0) on stop, edge-agent
  FDs returned to the pre-run baseline.
- NVDEC devfreq clock ranged 115-179 MHz. No thermal throttling; Tj 50.2-54.2 °C. System CPU and
  GR3D figures include the unrelated desktop session.

The run meets the R5A soak criteria: no unhandled crash, no zombie, no FD leak, no unbounded RSS
growth, no tight reconnect loop, and no unexplained media stop.

## Limitations

- The live source is a synthetic loopback fixture, not a camera. It is representative of RTSP/TCP
  interleaved transport and of H.264/H.265 hardware decode, not of camera encoders, Wi-Fi, WAN
  latency, or RTSPS/TLS (not exercised locally).
- `rtspsrc` opens a TCP connection to an RTSP redirect target before `before-send` can refuse the
  first request. No request or credential is sent, but the blind connect is a residual.
- One camera only; results must not be extrapolated linearly to ten cameras.
- Stall, reconnect, and renewal thresholds are qualification defaults that need tuning on real
  cameras.
