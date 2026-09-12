# ADR 0014: Official Ring WHEP live media

- Status: Accepted (R5A-R1), media plane blocked on a platform gate
- Date: 2026-09-11

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

**Media plane is gated.** `webrtcbin` is installed, but GStreamer's ICE agent lives in the libnice
elements from the Ubuntu package `gstreamer1.0-nice`, which is **not installed**. Measured on this
host: `webrtcbin` fails to reach PLAYING ("libnice elements are not available") and `create-offer`
returns an empty promise, so no SDP offer can be produced at all. `webrtc_media.probe_webrtc_runtime`
therefore fails closed with `WEBRTC_RUNTIME_UNAVAILABLE`, and the provider runs that probe
**before** requesting any token, so an unusable host never touches a Ring credential. The
webrtcbin pipeline (recvonly video-only -> depay -> parse -> `nvv4l2decoder` -> `fakesink`, fixed
application-controlled topology, no `parse_launch` on provider data, hardware decode only) is
specified here but deliberately not shipped unverified; installing the package is an explicit
separate gate.

**State mapping.** WebRTC states map onto the R5A machine: HTTP 201, SDP exchange, or ICE
connected are never `STREAMING`; only decoded-buffer progression is, exactly as for RTSP.

**No persistence.** Qualification uses a loopback synthetic WHEP endpoint with obviously synthetic
credentials; decoded media would terminate in `fakesink`. No frame, clip, screenshot, SDP body, or
session URL is written to disk, logs or reports.

## Consequences

VeoTrex can obtain and tear down official Ring WHEP sessions safely today, and the control plane is
qualified against a synthetic endpoint. Ring live media remains impossible on this Jetson until
`gstreamer1.0-nice` is installed under its own gate, and remains untested until developer
credentials, a linked test account and an HTTPS callback host exist. The unverified RTSPS path must
not be revived without independent current documentation of its authentication and session limits.
