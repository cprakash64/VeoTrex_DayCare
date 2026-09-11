# Camera provider contract

`CameraProvider` is an asynchronous protocol owned by the edge/provider boundary. It supports device
discovery, capability inspection, managed stream acquisition/closure, health, reconnect, recording
retrieval, and snapshots. A capability set independently represents `LIVE_VIDEO`, `RECEIVE_AUDIO`,
`SEND_AUDIO`, `SNAPSHOT`, `HISTORICAL_CLIP`, and `MOTION_EVENTS`; typed limitation entries record
constraints such as finite session duration. Device IDs are opaque and meaningful only with their
provider connection.

`open_stream` returns an async context manager so closure is deterministic on success, exception, or
cancellation. A handle exposes an opaque endpoint, transport, optional expiry, and stream ID; it does
not assume RTSP or an infinite session, and provider tokens never cross the boundary. Health returns
a stable status plus a sanitized detail code. Snapshot and recording bytes
are interface placeholders—the future media pipeline must stream bounded data rather than load large
payloads in memory before any production adapter is accepted.

Ring Partner API, ONVIF/RTSP, recorded-file, and simulated implementations can satisfy this protocol
without changing camera domain entities. An adapter owns authentication, secret resolution, rate
limits, transport negotiation, retries, and provider error mapping. General logs and metadata must
not expose URLs containing credentials or provider secrets.

R5A (ADR 0013) resolved the live-transport questions at the edge: the opaque `StreamHandle.endpoint`
string is superseded by an immutable, policy-validated `LiveSessionDescriptor` whose credential is
held separately in a `SessionCredential`; a fixed transport error taxonomy, a bounded reconnect
budget, overlap renewal with session generations, and an explicit media timestamp contract are
implemented in `veotrex_edge_agent.camera_transport`. Open questions remain for backpressure into
inference, recording streaming, clock alignment across cameras, capability refresh, and conformance
tests against a real provider adapter.
