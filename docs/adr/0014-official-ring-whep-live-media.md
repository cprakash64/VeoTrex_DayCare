# ADR 0014: Official Ring WHEP live media

- Status: Accepted (R5A-R1); WebRTC media runtime enabled and locally qualified in R5A-R2
- Date: 2026-09-11, updated 2026-09-12 (R5A-R2)

## Context

R5A qualified a provider-neutral transport (ADR 0013) but reported Ring as `NOT_CONFIGURED`: no
partner credentials, and the inherited Stage 1D-A RTSPS assumptions were unverified. R5A-R1
re-verified the **current** official Ring Developer material rather than trusting those
assumptions.

### Evidence (re-verified 2026-09-11)

Sources: `developer.ring.com`, `developer.amazon.com/docs/ring/api-documentation.html`,
`.../get-started.html`, `.../configure.html`, `.../ring-mcp.html`, and the official sample
`github.com/AmazonAppDev/ring-api-helloworld`. Ring also publishes a documentation MCP server
(`https://knowledge.appstore-mcp.ring.amazon.dev/mcp`); it is a documentation aid only and is
deliberately **not** a VeoTrex runtime dependency.

Verified current: app registration in the Ring Developer Console issuing Client ID, Client Secret
and HMAC Signature Key once; token endpoint `https://oauth.ring.com/oauth/token` with
`authorization_code` and `refresh_token` grants; ~4 h access tokens and ~30 d rotating refresh
tokens that must be refreshed proactively; API base `https://api.amazonvision.com`; device
discovery `GET /v1/devices`; one-way account linking via `?nonce=&time=` with HMAC-SHA256 over
`<time>:<account_id>`, URL-safe Base64 unpadded, 600 s window; scope `ava.v1:read` for
partner-initiated linking; portal URL fields (Account Link, Default Redirect, Token Exchange,
Webhook) that must be HTTPS and exact-match; 100 TPS rate limit with `Retry-After`; and WHEP live
video at `POST /v1/devices/{device_id}/media/streaming/whep/sessions` with
`Authorization: Bearer` and `Content-Type: application/sdp`, answer SDP in the body, session
resource in the `Location` header, teardown by `DELETE .../sessions/{session_id}`, and
`?component_id=N` for multi-camera devices. RTSPS remains documented at
`rtsps://video.rtsp.amazonvision.com:322/v1/devices/{device_id}/stream`.

Still **not documented** by Ring: WHEP success status code, Location format, ICE/STUN/TURN
requirements, trickle ICE or PATCH, session lifetime and renewal, codec names, RTSP authentication
method, and RTSPS session limits. None of these are filled in from Stage 1D-A memory.

## Decision

**WHEP is the primary Ring live-video path**, because it is what current official material and the
official sample demonstrate end to end. The legacy RTSPS path stays in the tree but is marked
unverified and can never be selected by default.

**Control plane** (`camera_transport/whep_client.py`), standard library only so the edge agent
gains no runtime dependency: HTTPS with verified certificates and hostname checking; a pinned
`api.amazonvision.com:443` origin; bounded connect/total timeouts, SDP size and header size;
`http.client`, which never follows redirects, with 3xx mapped to `REDIRECT_REFUSED` so a Bearer
token is never replayed to a redirect target; status mapped onto the taxonomy; answers validated
as SDP (`v=0`, a video `m=` line, no accepted non-video media, bounded, UTF-8, correct
`Content-Type`); `Location` validated for HTTPS, exact origin, session-path shape, no userinfo and
no fragment before it is ever used; and bounded authenticated `DELETE` teardown. Because Ring does
not document the success code, any 2xx carrying a valid answer is accepted rather than inventing
one; a missing `Location` is reported as "teardown unsupported", not guessed.

**Credential boundary.** The Bearer token exists only in an `Authorization` header. It is never in
a URL, query, argv, environment, log, metric, exception, report, or Git. `SessionCredential` gains
a `BEARER` mode whose value never appears in `repr`, `str`, or pickling, matching R5A.

**Session provider** (`camera_transport/whep_provider.py`) yields the R5A `LiveSessionLease`: a
descriptor pointing at the WHEP control endpoint (transport protocol `WHEP`) plus the separate
Bearer credential. It reuses the existing `TransportController`, state machine, health semantics,
timestamps, reconnect policy and metrics — there is no separate Ring streaming stack. Since Ring
documents no WHEP session lifetime, the descriptor carries no expiry, so the controller never
schedules a renewal it cannot justify.

**Media plane** (gated in R5A-R1, enabled in R5A-R2). `webrtcbin` ships with
`gstreamer1.0-plugins-bad`, but GStreamer's ICE agent lives in the libnice elements from
`gstreamer1.0-nice`. With only the `libnice10` C library installed, `webrtcbin` failed to reach
PLAYING ("libnice elements are not available") and `create-offer` returned an empty promise, so no
SDP offer could be produced at all. `webrtc_media.probe_webrtc_runtime` fails closed with
`WEBRTC_RUNTIME_UNAVAILABLE`, and `RingWhepSessionProvider` runs that probe **before** requesting a
token, so an unusable host never touches a Ring credential — that ordering remains in force.
R5A-R2 installed the reviewed one-package closure and the receive path is now implemented and
locally qualified: `webrtcbin` (recvonly, video-only) -> depay -> parse -> `nvv4l2decoder` ->
`fakesink`, a fixed application-controlled topology with no `parse_launch` on provider data and no
silent software fallback. The media worker is a separate `/usr/bin/python3 -I` process that never
receives the Bearer token: it emits the complete SDP offer upward, the parent performs the
authenticated exchange, and only the answer comes back down.

**State mapping.** WebRTC states map onto the R5A machine: HTTP 201, SDP exchange, or ICE
connected are never `STREAMING`; only decoded-buffer progression is, exactly as for RTSP.

**No persistence.** Qualification uses a loopback synthetic WHEP endpoint with obviously synthetic
credentials; decoded media would terminate in `fakesink`. No frame, clip, screenshot, SDP body, or
session URL is written to disk, logs or reports.

## Consequences

VeoTrex can obtain and tear down official Ring WHEP sessions safely today, the control plane is
qualified against a synthetic endpoint, and since R5A-R2 the WebRTC media runtime works end to end
on synthetic local media. Ring live video remains **untested and unqualified**: it still requires
developer credentials, a linked test account, a public HTTPS callback host, and a managed
credential vault. The unverified RTSPS path must not be revived without independent current
documentation of its authentication and session limits.

## R5A-R2: WebRTC runtime enablement and local media qualification (2026-09-12)

**Why one package was required.** Only `libnice10` (the C library) was present; the GStreamer
integration elements `nicesrc`/`nicesink` come from `gstreamer1.0-nice`. Without them webrtcbin
cannot build an ICE agent, which is why R5A-R1 reported `WEBRTC_RUNTIME_UNAVAILABLE`.

**Installed closure — exactly one package.** `gstreamer1.0-nice 0.1.21-2build3` (arm64, Ubuntu
noble/universe, `.deb` SHA-256 `1e82a717…b06dbbc4`). The simulated and actual transactions were
identical: 1 newly installed, 0 upgraded, 0 removed, 0 downgraded; all four declared dependencies
(`libc6`, `libglib2.0-0t64`, `libgstreamer1.0-0`, `libnice10`) were already satisfied. The operator
ran the command; Claude executed nothing privileged. Post-install the package diff against the
2083-entry baseline contains only that one line, `dpkg --audit` is empty, the six protected plugin
SHA-256 values are unchanged, and L4T 39.2.0 / kernel 6.8.12-1021-tegra / CUDA 13.2 / TensorRT
10.16.2.10 / GStreamer 1.24.2 / OpenCV / Docker / 25 W power mode are untouched.

**Runtime and ICE.** `nicesrc`, `nicesink` and `webrtcbin` all inspect (`libgstnice.so` 0.1.21);
the R5A-R1 probe now reports available. A recvonly video-only peer creates an offer, reaches ICE
gathering `complete`, and the offer re-serialized after gathering carries 6 **host** candidates
with fingerprint and ice-ufrag — WHEP requires a complete offer, so the implementation waits for
gathering rather than trickling. No STUN or TURN is configured: Ring documents no ICE contract, and
the sample's public STUN server is not treated as a requirement.

**Local media and hardware decode.** A synthetic sender (`videotestsrc` -> x264 -> RTP payloader ->
webrtcbin) negotiates with the production receive worker over deterministic in-process signaling.
Decoded output is `nvv4l2decoder` with `memory:NVMM` at 1280x720, first decoded buffer 2.3-2.8 s
after session start. Encode is software x264 because this Orin Nano has no NVENC.

**Measured results.** 15-minute soak: 13,498 decoded buffers at 15.037 FPS over a 897.6 s window;
inter-buffer gaps p50 66.66, p95 68.64, p99 70.31, max 95.45 ms; zero timestamp regressions, zero
stalls, zero dropped worker samples. Steady-state drift after the start-up ramp (78 samples):
worker RSS +36 KB, worker FDs 0, worker threads 0, edge-agent RSS +0.83 MB; process FDs returned to
baseline, no zombies, no child leak, worker exited rc 0, Tj <= 51.8 C with no throttling. Five
connect/disconnect cycles and three controlled receiver-worker kills all recovered (exit detected
in 0.02-0.04 s, first decoded again in 1.85-2.45 s) with zero stale-generation batches. The
negative matrix passes 7/7: malformed, empty, audio-bearing and unsupported-codec answers, a silent
sender, an injected absent-runtime probe, and a sender that disappears mid-stream. The WHEP
scenario drives the real control client into real media: POST then DELETE, Bearer in the
`Authorization` header only, validated session resource, successful teardown.

Two measurement defects found and fixed rather than tuned away: the qualification harness retained
every backend event (~110k objects, +14 MB over 15 minutes) and now folds events into bounded
counters; and the soak's growth check compared against a cold-start sample taken before the decode
chain exists, so it measured start-up ramp rather than drift and now measures post-ramp drift while
still reporting the raw ramp.

**Limits of this evidence.** Everything is synthetic `videotestsrc` media over loopback with host
candidates: it proves the runtime, the receive route, hardware decode and resource behaviour, but
says nothing about Ring's servers, real camera encoders, WAN latency, NAT traversal, TURN, or
session lifetime. These figures must never be quoted as Ring performance.
