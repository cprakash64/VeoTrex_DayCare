# ADR 0013: Provider-neutral live camera transport

- Status: Accepted (R5A)
- Date: 2026-09-11

## Context

The vision stack (YOLOX-S FP16 on TensorRT, ByteTrack-style tracking) was qualified on recorded
media. Live cameras need a transport layer that acquires authorized sessions, survives expiry,
stalls, and reconnects, and proves hardware decode, without the detector or tracker knowing which
provider supplied the frames. Earlier stages built the `CameraProvider` protocol (ADR 0004), Ring
one-way linking and the credential vault (ADR 0007), inventory/webhooks, and the Stage 1D-A Ring
RTSPS measurement harness. R5A qualifies transport only; decoded frames are not fed to inference.

## Decision

Add `veotrex_edge_agent.camera_transport`:

```text
LiveSessionProvider -> LiveSessionDescriptor (+ separate SessionCredential)
  -> TransportController (sans-I/O state machine) -> CameraTransportRunner (async executor)
  -> WorkerMediaBackend -> /usr/bin/python3 -I media_worker.py (GStreamer)
  -> rtspsrc (TCP interleaved) -> depay -> parse -> nvv4l2decoder -> fakesink
```

**Descriptor.** Immutable, bounded, and secret-free: provider kind, logical camera UUID, session
generation, a `ValidatedEndpoint`, monotonic creation/renew-after/expiry, codec hint, and bounded
capability names. Negotiated caps, not hints, select the codec path. Providers that report relative
lifetimes are converted to monotonic time at acquisition, so wall-clock and monotonic arithmetic
never mix. `safe_repr()` reveals only the scheme, a host class (or Ring's public host), and port.

**Endpoint policy.** Endpoints are deny-by-default: printable ASCII only, `rtsp`/`rtsps` only, no
userinfo, no fragment, no dot segments, bounded path/query alphabets, and per-provider host, port,
and path rules. Ring accepts only `rtsps://video.rtsp.amazonvision.com:322/v1/devices/{id}/stream`.
Generic cameras may use private LAN addresses; loopback, link-local (cloud metadata), multicast,
and unspecified addresses are rejected. The loopback fixture policy accepts only `127.0.0.1`.

**Secret contract.** `SessionCredential` never reprs, strs, or pickles its secret. The runner holds a
lease only between acquisition and backend start. The credential reaches GStreamer as the
`rtspsrc` `user-pw` property inside an isolated worker; it is sent once over an inherited
`AF_UNIX/SOCK_SEQPACKET` socketpair and never appears in argv, the environment (the worker gets
only `PATH`, `LANG`, and `LC_ALL`), a URL, a file, a log, a metric, or a report. `GST_DEBUG` never
reaches the worker. Worker errors are mapped onto the fixed taxonomy inside the worker; GStreamer
error text is inspected there and never forwarded.

**Pipeline construction.** Topology is fixed in `media_worker.py`; no `gst-launch`, `parse_launch`,
shell, or string concatenation handles provider data. Only the validated location, a bounded latency,
a bounded TCP timeout, and a decoder mode populate explicit properties. The worker re-validates the
START message. `select-stream` sets up video only.

**Redirects and control URLs.** `rtspsrc` follows RTSP 3xx redirects and trusts SDP control/
Content-Base URLs, re-presenting configured credentials. The worker's `before-send` hook refuses
any request that is not same-origin (scheme, host, port) with the validated endpoint and fails
closed with the terminal category `REDIRECT_REFUSED`. HTTP clients in the provider path keep
httpx's default of not following redirects.

**Worker isolation.** As in ADR 0008, system GStreamer/PyGObject stays out of the edge virtual
environment. Each session generation is one worker process with a new session group and
`PR_SET_PDEATHSIG`. `stop()` sends STOP, waits, kills if needed, reaps, shuts down the socket, and
joins the reader, so no zombie, orphan, or descriptor survives a session.

**State machine.** `STOPPED`, `CONNECTING`, `STREAMING`, `DEGRADED`, `RENEWING`, `RECONNECTING`,
`FAILED`, with an explicit transition table. `STREAMING` requires observed decoded-buffer
progress: a created process, a successful session response, or a completed DESCRIBE never implies
it. `FAILED` is terminal (non-retryable category or open circuit) until an explicit reset.

**Health layers.** `TransportHealth` reports provider state, camera availability, session
authorization, transport connection (SDP received), media flowing, and decoder health separately,
plus codec, decoder, hardware flag, generation, last PTS/arrival, recent rate, recent gap,
reconnect/renewal/stall counts, expiry remaining, last failure, and circuit state.

**Stall detection.** Gaps since the last decoded buffer are classified by configurable thresholds
(initial qualification values: jitter below 1 s, `DEGRADED` at 2 s, `MEDIA_STALLED` at 8 s, first
decoded buffer within 15 s). Compressed media arriving without decoded output for 5 s is
`DECODER_FAILED`, distinguishing decoder faults from transport stalls.

**Reconnect.** Bounded exponential backoff (1 s doubling to 30 s, ±20% jitter) with a sliding-
window circuit breaker (8 attempts per 600 s). The budget resets after 60 s of stable streaming or
by operator reset. Authorization, protocol, codec, endpoint, redirect, and not-configured failures
are terminal and never retried. A session that delivered media and then reached its provider
lifetime reconnects immediately without consuming the failure budget.

**Renewal and generations.** When a descriptor reaches `renew_after`, a replacement generation is
acquired while the current one keeps streaming. The first decoded buffer of the replacement
switches the stream and stops the old worker; replacement failures are bounded and retried while
the old session remains valid. If the old session expires first, the acquired replacement is
promoted and the blackout is counted as a renewal failure. Any buffer from a generation other than
the current one is rejected and counted, so stale media cannot re-enter the stream.

**Timestamp contract.** `MediaTimestamp` carries `stream_instance` (= generation), sequence, PTS,
monotonic arrival, source (`MEDIA_PTS` or `MISSING`), and a discontinuity flag set on the first
buffer of each instance and on PTS reversals over 500 ms. PTS is comparable only within one
instance. Arrival time is never silently substituted for a missing PTS.

**Observability.** Low-cardinality counters, gauges, and fixed-bucket histograms under
`veotrex_camera_*`. Labels are only the logical camera UUID and, for failures, the fixed taxonomy.
No API accepts free-form label values.

**No persistence.** Decoded buffers end in `fakesink` with `enable-last-sample=false`; probes record
timing metadata and sizes only. The fixture generates synthetic `videotestsrc` content in memory.

## R5A evidence

Details are in `docs/qualification/r5a-live-camera-transport.md`.

- **Generic transport: qualified on a synthetic loopback RTSP/TCP fixture.** H.264 1080p15 and
  H.265 720p15 decode on `nvv4l2decoder` into NVMM at the source rate, with the first decoded
  buffer in 0.6-0.9 s. Stall, reconnect, overlap renewal, and failure behavior matched the design,
  and no resource leaked.
- **Ring: not qualified.** The current official Ring Partner API page documents WebRTC/WHEP live
  video with RTSPS as an alternative, and snapshots. It does not state RTSPS session limits or how
  the token is presented to RTSP. No partner credentials, linked account, or managed vault are
  configured, so no Ring session was requested. The observed Ring transport is therefore
  unverified: the RTSPS path is implemented but unexercised, and WHEP is not implemented (the
  installed stack has `webrtcbin` but no `whepsrc`).
- **Residual:** `rtspsrc` performs a blind TCP connect to a redirect target before the first
  request is refused.

## Remaining limitation before live inference

A real camera transport suitable for the deployment must still be qualified: either an authorized
Ring account with partner credentials over RTSPS (with TLS, real session limits, and the token
convention verified), or a local RTSP/ONVIF camera. Only then should decoded buffers be sampled
into the R4 detector and tracker using the `MediaTimestamp` contract.

## Consequences

The detector and tracker can consume a provider-neutral decoded stream plus an explicit timing
contract. Ring, generic RTSP/ONVIF, and recorded sources differ only in their `LiveSessionProvider`
and endpoint policy. Costs: one worker process per active session (two during renewal overlap),
and qualification thresholds that still need tuning on real cameras.
