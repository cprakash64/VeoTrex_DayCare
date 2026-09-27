"""``RingWhepSource``: a Ring WHEP live session presented as a ``LiveVideoSource``.

The point of this stage is that there is nothing downstream to change. ``LocalCameraSource`` and
``RingWhepSource`` satisfy the same protocol and emit the same ``LiveFrame``, so the scheduler,
the YOLOX detector, ``PersonTracker``, the preview, occupancy and the timeline never learn which
one produced a frame - they cannot, because the only thing that distinguishes them is a
``SourceKind`` field the dashboard reads for display. No Ring-specific branch exists anywhere
below this module, and a test asserts that.

What this class owns: the lifecycle. Acquire a session, read decoded frames, publish
``LiveFrame``, and on the way out release the reader and then the session, on every path
including one where the consumer raises mid-iteration.

What it deliberately does not own: credentials. See ``ring_media`` - the provider holds the
token and performs both the create and the delete, and this source only ever holds an opaque
handle.

Failure is sorted into two kinds, because treating them alike is how a broken authorization
becomes an infinite retry loop against someone else's API:

  terminal     the answer will not change by asking again - not authorized, forbidden, camera
               not enrolled, codec unsupported, runtime missing. Fail immediately, once.
  transient    the answer might change - disconnect, stall, decoder restart, 5xx, rate limit.
               Retry under the existing ReconnectBudget, which is bounded by attempts and by a
               sliding window, and then stop.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import structlog

from veotrex_edge_agent.camera_transport.reconnect import ReconnectBudget, ReconnectPolicy
from veotrex_edge_agent.live.ring_media import (
    DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS,
    DEFAULT_STALL_TIMEOUT_SECONDS,
    DecodedFrame,
    RingLiveSessionProvider,
    RingMediaError,
    RingMediaMetrics,
    RingSessionMaterial,
)
from veotrex_edge_agent.live.source import (
    LiveFrame,
    LiveSourceError,
    SourceDescription,
    SourceHealth,
    SourceKind,
    validate_geometry,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from veotrex_edge_agent.live.ring_media import RingFrameReader

# A gap longer than this marks the next frame discontinuous, matching the USB source exactly so
# the tracker is told the same thing by both.
DISCONTINUITY_GAP_SECONDS = 1.0

# Categories that will not change by asking again. Everything else is treated as transient.
TERMINAL_CATEGORIES = frozenset(
    {
        "AUTHORIZATION_FAILED",
        "WHEP_HTTP_UNAUTHORIZED",
        "WHEP_HTTP_FORBIDDEN",
        "PROVIDER_NOT_CONFIGURED",
        "WEBRTC_RUNTIME_UNAVAILABLE",
        "CODEC_UNSUPPORTED",
        "WEBRTC_CODEC_UNSUPPORTED",
        "INVALID_ENDPOINT",
        "REDIRECT_REFUSED",
        # A 2xx whose lease cannot be addressed: asking again could only strand more leases
        # this node cannot DELETE. Terminal in the transport taxonomy too (errors.py).
        "WHEP_INVALID_LOCATION",
        "invalid_session_material",
        "provider_not_configured",
        "ring_session_material_missing",
    }
)


def is_terminal(category: str) -> bool:
    """Whether a failure category should end the session instead of being retried."""
    return category in TERMINAL_CATEGORIES


class RingWhepSource:
    """One Ring camera's live video, as frames.

    Nothing is opened at construction: a source can be built, inspected and closed on a machine
    with no Ring access at all, which is how every test below runs and how the demo behaves when
    Ring is unreachable.
    """

    kind = SourceKind.LIVE_RING_WHEP

    def __init__(
        self,
        camera_id: str,
        provider: RingLiveSessionProvider,
        reader: RingFrameReader,
        *,
        reconnect_policy: ReconnectPolicy | None = None,
        first_frame_timeout_seconds: float = DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS,
        stall_timeout_seconds: float = DEFAULT_STALL_TIMEOUT_SECONDS,
    ) -> None:
        if not camera_id:
            raise LiveSourceError("invalid_ring_camera_id")
        self._camera_id = camera_id
        self._provider = provider
        self._reader = reader
        self._policy = reconnect_policy or ReconnectPolicy()
        self._budget = ReconnectBudget(self._policy)
        self._first_frame_timeout = first_frame_timeout_seconds
        self._stall_timeout = stall_timeout_seconds
        self._health = SourceHealth.STARTING
        self._stopping = False
        self._material: RingSessionMaterial | None = None
        self._geometry: tuple[int, int] = (0, 0)
        self._frames_published = 0
        self._reconnects = 0
        self.metrics = RingMediaMetrics()
        self.failure_category: str | None = None
        self._logger = structlog.get_logger()

    # ------------------------------------------------------------------------- description
    @property
    def source_id(self) -> str:
        # The operator's own camera identifier, which is a local label, not a Ring account id
        # and not a device serial. It reaches the dashboard and the logs.
        return f"ring-camera-{self._camera_id}"

    @property
    def health(self) -> SourceHealth:
        return self._health

    @property
    def frames_captured(self) -> int:
        return self._frames_published

    @property
    def reconnect_count(self) -> int:
        return self._reconnects

    @property
    def media_frames_dropped_total(self) -> int:
        """Frames lost on this side of the decode worker before any consumer saw them.

        Geometry this source refused, plus frames the reader could not map or validate. Frames
        the worker's own bounded appsink discarded are not reported across the socket, so they
        are not in this number; it is a floor, not the whole of transport loss.
        """
        reader_dropped = 0
        with contextlib.suppress(Exception):
            reader_dropped = int(self._reader.stats.get("frames_dropped_total", 0))
        return self.metrics.frames_dropped_total + reader_dropped

    def describe(self) -> SourceDescription:
        width, height = self._geometry
        # No nominal fps: WHEP does not publish one, and a guessed rate on a dashboard is worse
        # than an absent one.
        return SourceDescription(self.kind, self.source_id, width, height, None)

    # ---------------------------------------------------------------------------- lifecycle
    def _acquire(self) -> RingSessionMaterial:
        self.metrics.sessions_started_total += 1
        try:
            material = self._provider.acquire()
        except RingMediaError:
            raise
        except LiveSourceError as exc:
            raise RingMediaError(exc.category) from None
        except Exception:
            # Never let a provider's own exception type or message escape: it may carry a URL,
            # a header or a body.
            raise RingMediaError("PROVIDER_UNAVAILABLE") from None
        if not isinstance(material, RingSessionMaterial):
            raise RingMediaError("ring_session_material_missing")
        return material

    def _release(self) -> None:
        """Teardown, in the order that cannot strand a remote session.

        The reader closes first so nothing is still decoding into a session that is about to
        disappear, then the session resource is deleted. Both are suppressed: a failure here
        must not replace the reason we are shutting down, and a release that raises would skip
        the release after it.
        """
        with contextlib.suppress(Exception):
            self._reader.close()
        material, self._material = self._material, None
        if material is not None:
            with contextlib.suppress(Exception):
                self._provider.release(material)
            self.metrics.sessions_completed_total += 1

    def _fail(self, category: str) -> None:
        self.failure_category = category
        self.metrics.session_failures_total += 1
        self.metrics.last_failure_category = category
        self._health = SourceHealth.FAILED
        self.metrics.stream_state = str(SourceHealth.FAILED)
        # Category only. No endpoint, no token, no SDP, no provider message.
        self._logger.warning(
            "ring_whep_session_failed", source_id=self.source_id, category=category
        )

    def close(self) -> None:
        self._stopping = True
        self._release()
        if self._health not in {SourceHealth.FAILED}:
            self._health = SourceHealth.STOPPED
        self.metrics.stream_state = str(self._health)

    def __enter__(self) -> RingWhepSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---------------------------------------------------------------------------- iteration
    def frames(self) -> Iterator[LiveFrame]:
        """Yield frames until the session ends, the budget runs out, or ``close`` is called.

        A terminal failure ends the iterator immediately. A transient one is retried under the
        bounded budget; when the budget is exhausted the iterator ends rather than raising, so a
        demo whose camera went away stops cleanly and the dashboard shows the failure instead of
        a traceback - the same contract ``LocalCameraSource`` offers.
        """
        session_start = time.monotonic_ns()
        index = 0
        previous_monotonic: int | None = None
        pending_discontinuity = False
        self._stopping = False
        try:
            while not self._stopping:
                try:
                    material = self._acquire()
                except RingMediaError as exc:
                    if is_terminal(exc.category) or not self._may_retry():
                        self._fail(exc.category)
                        return
                    # A 5xx, a rate limit or a camera that is briefly offline. Retryable, and
                    # the gap still breaks continuity for whatever comes next.
                    self.failure_category = exc.category
                    pending_discontinuity = True
                    self._health = SourceHealth.RECONNECTING
                    self.metrics.stream_state = "RECONNECTING"
                    continue
                self._material = material
                self._health = SourceHealth.STARTING
                self.metrics.stream_state = "CONNECTING"
                try:
                    self._reader.start(material)
                except Exception as exc:
                    category = getattr(exc, "category", "DECODER_START_FAILED")
                    self._release()
                    if is_terminal(str(category)) or not self._may_retry():
                        self._fail(str(category))
                        return
                    pending_discontinuity = True
                    continue

                self._health = SourceHealth.RUNNING
                self.metrics.stream_state = "STREAMING"
                timeout = self._first_frame_timeout
                stable_since: float | None = None
                ended_cleanly = False  # set by a clean EOS, or by the loop's else-clause
                while not self._stopping:
                    try:
                        decoded = self._reader.read(timeout)
                    except Exception as exc:
                        self.failure_category = str(getattr(exc, "category", "DECODER_FAILED"))
                        decoded = None
                        ended_cleanly = False
                        break
                    if decoded is None:
                        if getattr(self._reader, "eos", False):
                            # The stream ended. Not a fault, so not retried: reconnecting to a
                            # feed that finished would loop forever against a healthy provider.
                            ended_cleanly = True
                            break
                        # No frame inside the window and no EOS. Whether the socket thinks it
                        # is connected is not evidence: a feed that delivers nothing is not
                        # streaming.
                        self.failure_category = (
                            "FIRST_MEDIA_TIMEOUT" if index == 0 else "MEDIA_STALLED"
                        )
                        break
                    timeout = self._stall_timeout
                    frame = self._to_live_frame(
                        decoded,
                        index=index,
                        session_start=session_start,
                        pending_discontinuity=pending_discontinuity or decoded.discontinuity,
                    )
                    if frame is None:
                        # Geometry the pipeline will not accept. One bad frame is not a dead
                        # session, so it is dropped and counted rather than ending the stream.
                        self.metrics.frames_dropped_total += 1
                        continue
                    if stable_since is None:
                        stable_since = time.monotonic()
                    pending_discontinuity = False
                    previous_monotonic = decoded.arrival_monotonic_ns
                    index += 1
                    self._frames_published += 1
                    yield frame
                    if self._stable(stable_since):
                        self._budget.reset()
                else:
                    ended_cleanly = True

                self._release()
                if ended_cleanly or self._stopping:
                    break
                category = self.failure_category or "TRANSPORT_DISCONNECTED"
                if is_terminal(category) or not self._may_retry():
                    self._fail(category)
                    return
                # A reconnect breaks temporal continuity: motion across the gap is not motion.
                pending_discontinuity = True
                self._health = SourceHealth.RECONNECTING
                self.metrics.stream_state = "RECONNECTING"
        finally:
            self._release()
            if self._health not in {SourceHealth.FAILED}:
                self._health = SourceHealth.STOPPED
                self.metrics.stream_state = str(SourceHealth.STOPPED)
        _ = previous_monotonic  # continuity is carried by pending_discontinuity

    def _stable(self, stable_since: float | None) -> bool:
        return (
            stable_since is not None
            and self._budget.consecutive_failures > 0
            and time.monotonic() - stable_since >= self._policy.stable_reset_seconds
        )

    def _may_retry(self) -> bool:
        delay = self._budget.next_delay(time.monotonic())
        if delay is None:
            # Circuit open: attempts in the window are spent. Stopping is the bounded answer.
            return False
        self._reconnects += 1
        self.metrics.reconnect_count = self._reconnects
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not self._stopping:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return not self._stopping

    def _to_live_frame(
        self,
        decoded: DecodedFrame,
        *,
        index: int,
        session_start: int,
        pending_discontinuity: bool,
    ) -> LiveFrame | None:
        try:
            validate_geometry(decoded.width, decoded.height)
        except LiveSourceError:
            return None
        if decoded.image.ndim != 3 or decoded.image.shape[2] != 3:
            return None
        now = decoded.arrival_monotonic_ns
        self.metrics.frames_received_total += 1
        self.metrics.observe_age((time.monotonic_ns() - now) / 1e6)
        self._geometry = (decoded.width, decoded.height)
        return LiveFrame(
            kind=self.kind,
            source_id=self.source_id,
            frame_index=index,
            # Monotonic arrival drives continuity, exactly as the USB source does, so an NTP
            # step or a reconnect cannot move the timeline.
            timestamp_ms=(now - session_start) / 1e6,
            monotonic_ns=now,
            width=decoded.width,
            height=decoded.height,
            image=decoded.image,
            # The media clock is advisory and reported as-is, including absent. It is never
            # substituted for arrival time.
            capture_timestamp_ms=decoded.pts_ms,
            discontinuity=pending_discontinuity,
        )

    def snapshot(self) -> dict[str, Any]:
        """Ring-specific counters for the dashboard. Carries no identifier from the session."""
        merged = dict(self.metrics.as_dict())
        reader_stats = {}
        with contextlib.suppress(Exception):
            reader_stats = dict(self._reader.stats)
        for key, value in reader_stats.items():
            merged.setdefault(f"ring_{key}", value)
        return merged
