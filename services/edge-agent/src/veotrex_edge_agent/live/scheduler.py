"""Bounded-latency scheduling between capture and inference (V1-DEMO-01, V1-03A).

A camera delivers ~16-30 fps. The YOLOX-S TensorRT detector costs ~85-105 ms per frame on the
Jetson. Those two rates cannot both be honoured, and the choice of which to sacrifice *is* the
design:

**Newest-frame-wins.** Capture runs in its own thread and publishes into a single-slot buffer.
An arriving frame overwrites the one waiting, so the detector always works on the most recent
view of the room and never on a backlog.

The alternative - a FIFO queue that buffers every frame - is worse for this product in a way
that gets worse over time. At 16 fps in and 6 fps out, a queue accumulates ~10 frames a second;
after a minute the dashboard would be showing the room as it was a minute ago. **For safety
monitoring, a current view with gaps beats a complete view that is minutes behind.**

Memory follows from the same choice: at most one frame waits, one is being processed, and one
is being captured. Nothing grows with session length, so a camera left running all afternoon
costs what it costs in the first second.

**Paced inference (V1-03A).** Without a pacer the consumer takes a frame the instant it is free,
which runs the GPU flat out and leaves nothing for anything else on the device. With an
``AdaptiveInferencePacer`` the consumer takes the newest frame only when inference is *due*: at
a target rate, slowed down when measured inference cost says the target cannot be sustained,
and recovered slowly when it can again. Running YOLOX on every camera frame was never the goal;
running it on a *current* frame at a sustainable cadence is.

**What happens to a frame that is not processed** is counted, and counted by cause, because a
sampling decision and a failure are different facts:

``inference_frames_skipped_scheduler_total``
    Superseded in the slot while inference was *not yet due*. The pacer chose not to run
    inference on it; an idle detector would not have taken it either. Intentional sampling.

``inference_frames_dropped_backpressure_total`` (also ``frames_dropped_total``)
    Superseded while inference *was* due but the consumer was still busy with an earlier
    frame: the detector could not keep up with the schedule. With no pacer every supersession
    is counted here, because an unpaced consumer wants every frame it can get.

``inference_frames_discarded_on_stop_total``
    Still waiting in the slot when the scheduler was stopped.

So, always: ``captured = selected + skipped + backpressure + discarded_on_stop + waiting``,
where ``waiting`` is 0 or 1. Transport loss upstream of this module (decoder, socket, worker)
is not visible here and is reported by the source.

``capacity`` is configurable above 1 for a future source whose frames are individually
expensive to reacquire, but the default is 1 and the demo uses the default.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from veotrex_edge_agent.live.source import LiveFrame, LiveSourceError, SourceHealth
from veotrex_edge_agent.qualification.metrics import BoundedSamples, percentile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from veotrex_edge_agent.live.source import LiveVideoSource

# How long a consumer waits for a frame before checking whether it should still be running.
# Short enough that shutdown is prompt, long enough not to spin a core.
POLL_INTERVAL_SECONDS = 0.05
# How long ``stop`` waits for the capture thread to notice and unwind.
JOIN_TIMEOUT_SECONDS = 5.0
# Latency reservoirs kept by the scheduler itself. Fixed capacity, like every other sample set.
SCHEDULER_SAMPLE_CAPACITY = 512

# The single-camera Jetson operating band (V1-03A). Measured, not guessed: on the R39.2.1
# engine a real network-camera session showed detector p95 ~106 ms and pipeline p95
# ~117 ms. At 6 fps inference uses ~70 % of one inference slot and leaves headroom for
# spikes; 8 fps is the ceiling that still fits under a 1.25x safety margin at that cost;
# 3 fps is the floor below which a walking adult moves far enough between detections to
# strain IoU association.
DEFAULT_INFERENCE_FPS = 6.0
DEFAULT_MIN_INFERENCE_FPS = 3.0
DEFAULT_MAX_INFERENCE_FPS = 8.0


@dataclass(frozen=True, slots=True)
class InferenceRateConfig:
    """The inference cadence and how it adapts. Every bound is explicit and validated.

    ``target_fps`` is the preferred operating point and the rate used until enough cost has
    been measured. The pacer never schedules faster than ``target_fps`` (and so never faster
    than ``max_fps``); under load it slows toward ``min_fps``. If even ``min_fps`` cannot be
    sustained, the schedule holds at ``min_fps`` and inference simply runs back to back - the
    pacer cannot make the detector faster, and it says so in ``inference_scheduler_state``.
    """

    target_fps: float = DEFAULT_INFERENCE_FPS
    min_fps: float = DEFAULT_MIN_INFERENCE_FPS
    max_fps: float = DEFAULT_MAX_INFERENCE_FPS
    # The period must leave this much room over the measured p95 inference cost.
    safety_factor: float = 1.25
    # Recent per-inference service times the p95 is taken over.
    sample_window: int = 32
    # No adaptation until this many samples exist; the first inference is often a warm-up.
    min_samples: int = 5
    # At most one change of rate per interval, so the rate cannot chatter.
    adjust_interval_seconds: float = 1.0
    # Recovery is gradual: the period may shrink by at most this fraction per adjustment.
    recovery_step: float = 0.10
    # Changes smaller than this fraction of the current period are ignored.
    hysteresis: float = 0.05

    def __post_init__(self) -> None:
        if not 0.5 <= self.min_fps <= self.target_fps <= self.max_fps <= 30.0:
            raise ValueError("inference fps must satisfy 0.5 <= min <= target <= max <= 30")
        if not 1.0 <= self.safety_factor <= 4.0:
            raise ValueError("inference safety factor must be between 1.0 and 4.0")
        if not 4 <= self.sample_window <= 512:
            raise ValueError("inference sample window must be between 4 and 512")
        if not 1 <= self.min_samples <= self.sample_window:
            raise ValueError("inference min samples must be between 1 and the sample window")
        if not 0.1 <= self.adjust_interval_seconds <= 60.0:
            raise ValueError("inference adjust interval must be between 0.1 and 60 seconds")
        if not 0.0 < self.recovery_step <= 0.5:
            raise ValueError("inference recovery step must be in (0, 0.5]")
        if not 0.0 <= self.hysteresis < 0.5:
            raise ValueError("inference hysteresis must be in [0, 0.5)")

    @property
    def target_period(self) -> float:
        return 1.0 / self.target_fps

    @property
    def shortest_period(self) -> float:
        return 1.0 / self.max_fps

    @property
    def longest_period(self) -> float:
        return 1.0 / self.min_fps


class AdaptiveInferencePacer:
    """Decides *when* the next inference may start. Pure: no threads, no sleeping, no clock.

    Every method takes ``now`` from the caller, so under a fake clock the whole controller is
    deterministic. Not thread-safe on its own; ``BackpressureScheduler`` calls it under its lock.

    The rule, applied at most once per ``adjust_interval_seconds``::

        desired = clamp(max(1/target, p95(service) * safety), 1/max, 1/min)
        desired >  period * (1 + hysteresis)  ->  period = desired            (slow down now)
        desired <  period * (1 - hysteresis)  ->  period = max(desired,
                                                              period * (1 - recovery_step))
        otherwise                             ->  unchanged

    Slowing down is immediate because running over budget is what builds latency. Speeding up
    is gradual because one quiet second is not evidence the load has gone. Both are bounded.
    """

    def __init__(self, config: InferenceRateConfig | None = None) -> None:
        self.config = config or InferenceRateConfig()
        self.period = self.config.target_period
        self._samples: deque[float] = deque(maxlen=self.config.sample_window)
        self._last_selected: float | None = None
        self._last_adjusted: float | None = None
        self.adjustments_total = 0

    @property
    def scheduled_fps(self) -> float:
        return 1.0 / self.period

    @property
    def service_p95_seconds(self) -> float | None:
        return percentile(self._samples, 95)

    def due_in(self, now: float) -> float:
        """Seconds until inference may start; 0.0 when it may start now."""
        if self._last_selected is None:
            return 0.0
        return max(0.0, self._last_selected + self.period - now)

    def on_selected(self, now: float) -> None:
        """A frame was taken for inference at ``now``; the next is due one period later."""
        self._last_selected = now

    def expedite(self) -> None:
        """Make the next frame due immediately. Used for the first frame after a discontinuity,
        so a reconnected camera reaches the tracker without waiting out a period."""
        self._last_selected = None

    def observe(self, service_seconds: float, now: float) -> None:
        """Record what one inference cost, and adapt the period if the rule says so."""
        if not service_seconds >= 0.0:  # also rejects NaN
            return
        self._samples.append(service_seconds)
        if len(self._samples) < self.config.min_samples:
            return
        if (
            self._last_adjusted is not None
            and now - self._last_adjusted < self.config.adjust_interval_seconds
        ):
            return
        self._last_adjusted = now
        p95 = self.service_p95_seconds
        assert p95 is not None  # min_samples >= 1
        config = self.config
        desired = max(config.target_period, p95 * config.safety_factor)
        desired = min(max(desired, config.shortest_period), config.longest_period)
        if desired > self.period * (1.0 + config.hysteresis):
            self.period = desired
            self.adjustments_total += 1
        elif desired < self.period * (1.0 - config.hysteresis):
            self.period = max(desired, self.period * (1.0 - config.recovery_step))
            self.adjustments_total += 1

    @property
    def state(self) -> str:
        """``WARMING_UP``, ``AT_TARGET``, ``ADAPTED`` or ``DETECTOR_BELOW_MINIMUM``."""
        if len(self._samples) < self.config.min_samples:
            return "WARMING_UP"
        p95 = self.service_p95_seconds
        if p95 is not None and p95 > self.config.longest_period:
            # The detector alone takes longer than the slowest permitted period.
            return "DETECTOR_BELOW_MINIMUM"
        if self.period > self.config.target_period * (1.0 + 1e-9):
            return "ADAPTED"
        return "AT_TARGET"

    def snapshot(self) -> dict[str, Any]:
        p95 = self.service_p95_seconds
        return {
            "inference_pacing": "adaptive",
            "inference_target_fps": self.config.target_fps,
            "inference_min_fps": self.config.min_fps,
            "inference_max_fps": self.config.max_fps,
            "inference_scheduled_fps": round(self.scheduled_fps, 3),
            "inference_rate_adjustments_total": self.adjustments_total,
            "inference_scheduler_state": self.state,
            "inference_service_p95_window_ms": None if p95 is None else round(p95 * 1000.0, 3),
        }


def _summary(samples: BoundedSamples) -> dict[str, float | None]:
    return {
        "count": float(samples.count),
        "p50": samples.percentile(50),
        "p95": samples.percentile(95),
        "max": samples.maximum,
    }


def _rate(count: int, first: float | None, last: float | None) -> float:
    """Events per second across the span from the first to the last event."""
    if count < 2 or first is None or last is None or last <= first:
        return 0.0
    return (count - 1) / (last - first)


@dataclass(slots=True)
class SchedulerMetrics:
    frames_captured_total: int = 0
    # Frames handed to the consumer for inference ("selected").
    frames_delivered_total: int = 0
    # True pipeline loss: superseded while inference was due and busy. See the module docstring.
    frames_dropped_total: int = 0
    frames_skipped_scheduler_total: int = 0
    frames_discarded_on_stop_total: int = 0
    source_discontinuities_total: int = 0
    queue_depth_max: int = 0
    # Set once, when capture begins. ``capture_seconds`` is derived from it rather than only
    # assigned when the loop ends: an earlier version assigned elapsed time in the capture
    # thread's ``finally``, so a running demo reported 0.0 capture FPS on the dashboard while
    # frames_captured_total was visibly climbing. Live numbers have to be live.
    capture_started_monotonic: float | None = None
    capture_finished_seconds: float | None = None
    # Rates are measured between first and latest event, not from when capture was asked to
    # start: a network camera can spend seconds negotiating before its first frame, and
    # folding that into the denominator understates the steady-state rate.
    first_frame_at: float | None = None
    last_frame_at: float | None = None
    first_selected_at: float | None = None
    last_selected_at: float | None = None
    # Per-inference service time: from handing a frame over to being asked for the next one,
    # so detector + tracker + preview + publish. What the pacer adapts to.
    service_ms: BoundedSamples = field(
        default_factory=lambda: BoundedSamples(SCHEDULER_SAMPLE_CAPACITY)
    )
    # How long the selected frame had waited in the slot. Bounded by roughly one source
    # interval when the pacer is working; a growing value would mean a backlog.
    handoff_age_ms: BoundedSamples = field(
        default_factory=lambda: BoundedSamples(SCHEDULER_SAMPLE_CAPACITY)
    )
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    @property
    def capture_seconds(self) -> float:
        """Elapsed capture time: measured so far while running, final once stopped."""
        if self.capture_finished_seconds is not None:
            return self.capture_finished_seconds
        if self.capture_started_monotonic is None:
            return 0.0
        return max(self.clock() - self.capture_started_monotonic, 0.0)

    @property
    def capture_fps(self) -> float:
        """Arrival rate between the first and the latest captured frame."""
        return _rate(self.frames_captured_total, self.first_frame_at, self.last_frame_at)

    @property
    def effective_inference_fps(self) -> float:
        """Rate at which frames were actually taken for inference."""
        return _rate(self.frames_delivered_total, self.first_selected_at, self.last_selected_at)

    def record_arrival(self, now: float) -> None:
        if self.first_frame_at is None:
            self.first_frame_at = now
        self.last_frame_at = now

    def record_selection(self, now: float, arrived_at: float) -> None:
        self.frames_delivered_total += 1
        if self.first_selected_at is None:
            self.first_selected_at = now
        self.last_selected_at = now
        self.handoff_age_ms.add(max(0.0, now - arrived_at) * 1000.0)

    def snapshot(self) -> dict[str, Any]:
        return {
            # Original names, kept for every existing consumer of this snapshot.
            "frames_captured_total": self.frames_captured_total,
            "frames_delivered_total": self.frames_delivered_total,
            "frames_dropped_total": self.frames_dropped_total,
            "camera_capture_fps": round(self.capture_fps, 3),
            "capture_seconds": round(self.capture_seconds, 2),
            # V1-03A names. Aliases where the meaning is identical, so both read the same.
            "camera_frames_captured_total": self.frames_captured_total,
            "inference_frames_selected_total": self.frames_delivered_total,
            "inference_frames_skipped_scheduler_total": self.frames_skipped_scheduler_total,
            "inference_frames_dropped_backpressure_total": self.frames_dropped_total,
            "inference_frames_discarded_on_stop_total": self.frames_discarded_on_stop_total,
            "inference_queue_depth_max": self.queue_depth_max,
            "effective_inference_fps": round(self.effective_inference_fps, 3),
            "source_discontinuities_total": self.source_discontinuities_total,
            "inference_service_ms": _summary(self.service_ms),
            "inference_handoff_age_ms": _summary(self.handoff_age_ms),
        }


class BackpressureScheduler:
    """Runs a source on a capture thread and hands the newest frame to the consumer.

    Used as a context manager so the thread and the device are released on every path:

        with BackpressureScheduler(source, pacer=AdaptiveInferencePacer()) as scheduler:
            for frame in scheduler.frames():
                ...

    ``pacer`` is optional. Without one the consumer takes frames as fast as it can, which is the
    V1-DEMO-01 behaviour and what the recorded-style tests rely on. ``clock`` exists so tests
    can drive the timing decisions deterministically; production uses ``time.monotonic``.
    """

    def __init__(
        self,
        source: LiveVideoSource,
        *,
        capacity: int = 1,
        pacer: AdaptiveInferencePacer | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise LiveSourceError("invalid_scheduler_capacity")
        self._source = source
        self._pacer = pacer
        self._clock = clock
        # (frame, arrival time on ``clock``). Bounded by maxlen; nothing else holds frames.
        self._buffer: deque[tuple[LiveFrame, float]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: str | None = None
        # True from handing a frame to the consumer until it asks for the next one.
        self._busy = False
        # Set by ``stop``: the consumer returns at once instead of draining the slot.
        self._closing = False
        self.metrics = SchedulerMetrics(clock=clock)
        self._logger = structlog.get_logger()

    # ------------------------------------------------------------------------------ status
    @property
    def failure(self) -> str | None:
        """The bounded category that stopped capture, or None."""
        return self._failure

    @property
    def health(self) -> SourceHealth:
        return self._source.health

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def capacity(self) -> int:
        return self._buffer.maxlen or 0

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._buffer)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self.metrics.snapshot()
            snapshot["inference_queue_capacity"] = self.capacity
            snapshot["inference_queue_depth"] = len(self._buffer)
            if self._pacer is not None:
                snapshot.update(self._pacer.snapshot())
            else:
                snapshot["inference_pacing"] = "unpaced"
        return snapshot

    # ------------------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread is not None or self._closing:
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._capture_loop, name="veotrex-live-capture", daemon=True
        )
        self._thread = thread
        thread.start()

    def _capture_loop(self) -> None:
        started = self._clock()
        self.metrics.capture_started_monotonic = started
        try:
            for frame in self._source.frames():
                if self._stop.is_set():
                    break
                self._admit(frame)
                self._arrived.set()
        except LiveSourceError as exc:
            self._failure = exc.category
            self._logger.warning("live_capture_failed", category=exc.category)
        except Exception:
            # A driver can raise anything; the category stays bounded either way.
            self._failure = "capture_error"
            self._logger.warning("live_capture_failed", category="capture_error")
        finally:
            self.metrics.capture_finished_seconds = self._clock() - started
            self._stop.set()
            # Wake a consumer blocked waiting for a frame that will never arrive.
            self._arrived.set()

    def _admit(self, frame: LiveFrame) -> None:
        """Place one captured frame in the slot, classifying whatever it supersedes."""
        with self._lock:
            now = self._clock()
            self.metrics.record_arrival(now)
            carry_discontinuity = False
            if len(self._buffer) == self._buffer.maxlen:
                superseded, _ = self._buffer[0]
                # A superseded frame must not take a reconnect with it: the next frame the
                # tracker sees would otherwise look continuous with state from before the gap.
                carry_discontinuity = bool(getattr(superseded, "discontinuity", False))
                due = self._pacer is None or self._pacer.due_in(now) <= 0.0
                if self._pacer is None or (self._busy and due):
                    self.metrics.frames_dropped_total += 1
                else:
                    self.metrics.frames_skipped_scheduler_total += 1
            if getattr(frame, "discontinuity", False):
                self.metrics.source_discontinuities_total += 1
                if self._pacer is not None:
                    self._pacer.expedite()
            self._buffer.append((frame, now))
            if carry_discontinuity:
                head, arrived_at = self._buffer[0]
                if not getattr(head, "discontinuity", False) and dataclasses.is_dataclass(head):
                    # A new frame object over the same pixels: no image copy.
                    self._buffer[0] = (dataclasses.replace(head, discontinuity=True), arrived_at)
            self.metrics.frames_captured_total += 1
            self.metrics.queue_depth_max = max(self.metrics.queue_depth_max, len(self._buffer))

    # ---------------------------------------------------------------------------- consumer
    def frames(self, *, max_frames: int | None = None) -> Iterator[LiveFrame]:
        """Yield the newest available frame whenever inference is due.

        Ends when capture stops and the slot drains, when ``stop`` is called, or after
        ``max_frames``. The consumer is considered busy from each yield until it asks for the
        next frame, and that span is the service time the pacer adapts to.
        """
        self.start()
        delivered = 0
        while True:
            if max_frames is not None and delivered >= max_frames:
                return
            if self._closing:
                return
            frame: LiveFrame | None = None
            wait = POLL_INTERVAL_SECONDS
            with self._lock:
                now = self._clock()
                due_in = 0.0 if self._pacer is None else self._pacer.due_in(now)
                if self._buffer and due_in <= 0.0:
                    frame, arrived_at = self._buffer.popleft()
                    self._busy = True
                    self.metrics.record_selection(now, arrived_at)
                    if self._pacer is not None:
                        self._pacer.on_selected(now)
                else:
                    if not self._buffer and self._stop.is_set():
                        # Capture has finished and the slot is empty.
                        return
                    # Cleared under the lock, so an arrival between the check above and the
                    # wait below cannot be missed: the capture thread sets the event only
                    # after it has appended, and it cannot append while this lock is held.
                    self._arrived.clear()
                    if self._buffer:
                        wait = min(due_in, POLL_INTERVAL_SECONDS)
            if frame is None:
                self._arrived.wait(wait)
                continue
            delivered += 1
            started = self._clock()
            try:
                yield frame
            finally:
                with self._lock:
                    self._busy = False
            # Reached only when the consumer comes back for another frame, i.e. it finished
            # this one. An abandoned frame (generator closed) records no service time.
            with self._lock:
                finished = self._clock()
                service = max(0.0, finished - started)
                self.metrics.service_ms.add(service * 1000.0)
                if self._pacer is not None:
                    self._pacer.observe(service, finished)

    # ----------------------------------------------------------------------------- closing
    def stop(self) -> None:
        """Stop capture and release the source. Idempotent, and safe before ``start``."""
        self._closing = True
        self._stop.set()
        self._arrived.set()
        # Closing the source is what actually unblocks a capture thread sitting in a blocking
        # device read; the stop flag alone would only be noticed between frames.
        try:
            self._source.close()
        except Exception:  # pragma: no cover - close must not mask the caller's error
            self._logger.warning("live_source_close_failed")
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():  # pragma: no cover - a wedged driver
                self._logger.warning("live_capture_thread_did_not_exit")
        with self._lock:
            self.metrics.frames_discarded_on_stop_total += len(self._buffer)
            self._buffer.clear()

    def __enter__(self) -> BackpressureScheduler:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
