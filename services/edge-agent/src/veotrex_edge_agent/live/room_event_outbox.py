"""Durable, bounded outbox and uploader for anonymous room-transition events (V1-05B).

**Flow.** A transition emitted by the ``PortalMonitor`` becomes a payload with a fresh random
``event_id`` and is written to a local SQLite outbox *before* any upload is attempted. The
uploader sends the oldest events first, in batches, to ``POST /v1/edge/events/room-transitions``,
and removes each one only when the control plane has acknowledged it (ACCEPTED or DUPLICATE) or
has refused it permanently (REJECTED, then kept in a bounded dead-letter table). The
``event_id`` never changes across retries or restarts, and the server's primary key on it turns
any retry into DUPLICATE: one logical event, one row. Enqueuing the same ``event_id`` twice is a
no-op, not a second event.

**Durable and bounded.** SQLite (standard library) in WAL mode with ``synchronous=FULL``: an
acknowledged enqueue survives a crash or power loss, and a restart resumes where it stopped. The
file is 0600 in a 0700 directory owned by this user; the WAL is truncated back to at most 1 MiB
after each checkpoint. At most ``capacity`` events (default 10 000) of at most
``MAX_PAYLOAD_BYTES`` (512) each are held - about 5 MiB of payload at the default (a real event is
about 350 bytes) - plus at most ``DEAD_LETTER_CAPACITY`` refused events. When the outbox is full a
new event is refused and counted (``room_transition_queue_dropped_total``); the caller never
blocks, so inference is never held up. A payload is ids, a type, a time and geometry: exactly
``PAYLOAD_KEYS``, checked on enqueue. No credential, image, frame, crop, embedding or identity
can enter it.

**Retry policy.** Transport failures, 5xx, 429 and "route not there" (404/405/408) back off
exponentially from 2 s to at most 5 minutes, with bounded jitter (injectable for tests).
401/403 - and a missing credential file or a refused redirect - mean the node's configuration is
wrong: they wait the maximum and are counted, never retried hot, and never discard an event. A
batch refused as malformed (400/413/422) is retried one event at a time, so one bad event cannot
hold the others; a single event refused that way, or one the server answers REJECTED, moves to
the dead-letter table with a bounded category instead of being retried forever.
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import structlog

from veotrex_edge_agent.edge_control_plane import (
    ROOM_TRANSITIONS_PATH,
    ControlPlaneError,
    EdgeControlPlaneClient,
)
from veotrex_edge_agent.live.managed_config import ensure_private_directory
from veotrex_edge_agent.live.portal_crossing import RoomTransition, RoomTransitionKind

DEFAULT_CAPACITY = 10_000
MAX_CAPACITY = 100_000
DEAD_LETTER_CAPACITY = 1_000
MAX_PAYLOAD_BYTES = 512
BATCH_SIZE = 50
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 300.0
POLL_SECONDS = 2.0
WAL_SIZE_LIMIT_BYTES = 1_048_576
OUTBOX_FILE_NAME = "room-transitions.sqlite3"
PAYLOAD_KEYS = frozenset(
    {
        "event_id",
        "camera_id",
        "portal_id",
        "event_type",
        "occurred_at",
        "ephemeral_track_id",
        "stream_instance_id",
        "crossing_x",
        "crossing_y",
        "evidence_observations",
    }
)
EVENT_TYPES = frozenset(str(kind) for kind in RoomTransitionKind)
# The request can never succeed as sent: split the batch, then dead-letter the one event.
PERMANENT_CATEGORIES = frozenset({"request_rejected", "request_too_large"})
# The node itself is misconfigured (credential, assignment, origin): wait long, keep the event.
CONFIGURATION_CATEGORIES = frozenset(
    {"auth_rejected", "credential_unavailable", "redirect_refused", "path_not_allowed"}
)
_CATEGORY = re.compile(r"^[a-z][a-z_]{0,47}$")

_logger = structlog.get_logger("veotrex.edge.room_events")


def event_payload(
    transition: RoomTransition,
    *,
    camera_id: str,
    stream_instance_id: str,
    occurred_at_unix: float,
    event_id: UUID | None = None,
) -> dict[str, Any]:
    """The exact upload payload for one transition. The event id is fixed here, once."""
    return {
        "event_id": str(event_id or uuid4()),
        "camera_id": camera_id,
        "portal_id": transition.portal_id,
        "event_type": str(transition.kind),
        "occurred_at": datetime.fromtimestamp(occurred_at_unix, UTC).isoformat(),
        "ephemeral_track_id": int(transition.track_id),
        "stream_instance_id": stream_instance_id,
        "crossing_x": round(float(transition.crossing_point[0]), 6),
        "crossing_y": round(float(transition.crossing_point[1]), 6),
        "evidence_observations": int(transition.evidence_observations),
    }


def _valid_payload(payload: dict[str, Any]) -> bool:
    """Exactly the anonymous fields, each of the expected type - nothing else can be queued."""
    if set(payload) != PAYLOAD_KEYS:
        return False
    try:
        for key in ("event_id", "camera_id", "portal_id"):
            if not isinstance(payload[key], str) or str(UUID(payload[key])) != payload[key]:
                return False
    except ValueError:
        return False
    return (
        payload["event_type"] in EVENT_TYPES
        and isinstance(payload["occurred_at"], str)
        and isinstance(payload["stream_instance_id"], str)
        and all(
            isinstance(payload[key], int) and not isinstance(payload[key], bool)
            for key in ("ephemeral_track_id", "evidence_observations")
        )
        and all(
            isinstance(payload[key], float) and 0.0 <= payload[key] <= 1.0
            for key in ("crossing_x", "crossing_y")
        )
    )


@dataclass(slots=True)
class OutboxMetrics:
    room_transition_events_generated_total: int = 0
    room_transition_events_queued_total: int = 0
    room_transition_events_duplicate_enqueue_total: int = 0
    room_transition_events_uploaded_total: int = 0
    room_transition_events_duplicate_ack_total: int = 0
    room_transition_events_rejected_total: int = 0
    room_transition_queue_dropped_total: int = 0
    room_transition_dead_letter_evicted_total: int = 0
    room_transition_upload_failures_total: int = 0
    room_transition_auth_failures_total: int = 0
    last_upload_failure_category: str | None = None


class RoomEventOutbox:
    def __init__(self, directory: Path, *, capacity: int = DEFAULT_CAPACITY) -> None:
        if not 1 <= capacity <= MAX_CAPACITY:
            raise ValueError(f"outbox capacity must be 1-{MAX_CAPACITY}")
        self.directory = ensure_private_directory(directory)
        self.path = self.directory / OUTBOX_FILE_NAME
        self.capacity = capacity
        self.metrics = OutboxMetrics()
        self._lock = threading.Lock()
        self._closed = False
        previous = os.umask(0o077)
        try:
            self._db = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None
            )
            os.chmod(self.path, 0o600)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT_BYTES}")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS outbox ("
                " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                " event_id TEXT NOT NULL UNIQUE,"
                " payload TEXT NOT NULL,"
                " attempts INTEGER NOT NULL DEFAULT 0,"
                " next_attempt_at REAL NOT NULL DEFAULT 0,"
                " solo INTEGER NOT NULL DEFAULT 0)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS dead_letter ("
                " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                " event_id TEXT NOT NULL UNIQUE,"
                " category TEXT NOT NULL,"
                " payload TEXT NOT NULL,"
                " dead_at REAL NOT NULL)"
            )
        finally:
            os.umask(previous)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._db.close()

    def _count(self, table: str) -> int:
        return int(self._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608

    def depth(self) -> int:
        with self._lock:
            return 0 if self._closed else self._count("outbox")

    def dead_letter_depth(self) -> int:
        with self._lock:
            return 0 if self._closed else self._count("dead_letter")

    def enqueue(self, payload: dict[str, Any]) -> bool:
        """Persist one event. False - never an exception, never a wait - when it is refused.

        Re-enqueuing an event id that is already queued is accepted and changes nothing.
        """
        with self._lock:
            self.metrics.room_transition_events_generated_total += 1
            text = (
                json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
                if _valid_payload(payload)
                else ""
            )
            if not text or len(text) > MAX_PAYLOAD_BYTES or self._closed:
                self.metrics.room_transition_queue_dropped_total += 1
                return False
            try:
                if self._db.execute(
                    "SELECT 1 FROM outbox WHERE event_id = ?", (payload["event_id"],)
                ).fetchone():
                    self.metrics.room_transition_events_duplicate_enqueue_total += 1
                    return True
                if self._count("outbox") >= self.capacity:
                    self.metrics.room_transition_queue_dropped_total += 1
                    return False
                self._db.execute(
                    "INSERT INTO outbox (event_id, payload) VALUES (?, ?)",
                    (payload["event_id"], text),
                )
            except sqlite3.Error:
                self.metrics.room_transition_queue_dropped_total += 1
                return False
            self.metrics.room_transition_events_queued_total += 1
            return True

    def due(self, now: float, limit: int = BATCH_SIZE) -> list[tuple[str, dict[str, Any]]]:
        """The oldest events, in order, if the oldest is due. A solo-flagged head goes alone."""
        with self._lock:
            if self._closed:
                return []
            head = self._db.execute(
                "SELECT next_attempt_at, solo FROM outbox ORDER BY seq LIMIT 1"
            ).fetchone()
            if head is None or head[0] > now:
                return []
            size = 1 if head[1] else limit
            rows = self._db.execute(
                "SELECT event_id, payload FROM outbox ORDER BY seq LIMIT ?", (size,)
            ).fetchall()
        return [(str(event_id), json.loads(payload)) for event_id, payload in rows]

    def remove(self, event_ids: list[str]) -> None:
        with self._lock:
            if not self._closed:
                self._db.executemany(
                    "DELETE FROM outbox WHERE event_id = ?", [(i,) for i in event_ids]
                )

    def defer(self, event_ids: list[str], until: float, *, solo: bool = False) -> None:
        with self._lock:
            if not self._closed:
                self._db.executemany(
                    "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?,"
                    " solo = MAX(solo, ?) WHERE event_id = ?",
                    [(until, 1 if solo else 0, i) for i in event_ids],
                )

    def dead_letter(self, event_ids: list[str], category: str, now: float) -> None:
        """Move permanently refused events out of the queue, keeping the newest
        ``DEAD_LETTER_CAPACITY`` of them for inspection. One transaction per call."""
        reason = category if _CATEGORY.fullmatch(category) else "rejected"
        with self._lock:
            if self._closed:
                return
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for event_id in event_ids:
                    self._db.execute(
                        "INSERT OR IGNORE INTO dead_letter (event_id, category, payload, dead_at)"
                        " SELECT event_id, ?, payload, ? FROM outbox WHERE event_id = ?",
                        (reason, now, event_id),
                    )
                    self._db.execute("DELETE FROM outbox WHERE event_id = ?", (event_id,))
                excess = self._count("dead_letter") - DEAD_LETTER_CAPACITY
                if excess > 0:
                    self._db.execute(
                        "DELETE FROM dead_letter WHERE seq IN"
                        " (SELECT seq FROM dead_letter ORDER BY seq LIMIT ?)",
                        (excess,),
                    )
                    self.metrics.room_transition_dead_letter_evicted_total += excess
                self._db.execute("COMMIT")
            except sqlite3.Error:
                self._db.execute("ROLLBACK")
                raise
            self.metrics.room_transition_events_rejected_total += len(event_ids)

    def dead_letters(self) -> list[tuple[str, str]]:
        """(event_id, category), oldest first. For diagnosis; no payload leaves here."""
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT event_id, category FROM dead_letter ORDER BY seq"
            ).fetchall()
        return [(str(event_id), str(category)) for event_id, category in rows]

    def attempts(self, event_id: str) -> int:
        with self._lock:
            if self._closed:
                return 0
            row = self._db.execute(
                "SELECT attempts FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
        return 0 if row is None else int(row[0])

    def record(self, **changes: int) -> None:
        """Add to counters under the lock (the uploader and the pipeline share them)."""
        with self._lock:
            for name, amount in changes.items():
                setattr(self.metrics, name, getattr(self.metrics, name) + amount)

    def note_failure(self, category: str | None) -> None:
        with self._lock:
            self.metrics.last_upload_failure_category = category

    def snapshot(self) -> dict[str, Any]:
        """Counters and depths only - no event content, no track numbers."""
        with self._lock:
            metrics = asdict(self.metrics)
            closed = self._closed
            depth = 0 if closed else self._count("outbox")
            dead = 0 if closed else self._count("dead_letter")
        return {
            **metrics,
            "room_transition_queue_depth": depth,
            "room_transition_queue_capacity": self.capacity,
            "room_transition_dead_letter_depth": dead,
            "room_transition_dead_letter_capacity": DEAD_LETTER_CAPACITY,
        }


def backoff_seconds(attempts: int, jitter: Callable[[float], float]) -> float:
    """2, 4, 8 ... capped at 300 s, plus bounded non-negative jitter (at most a quarter)."""
    delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * float(2 ** max(0, min(attempts, 16))))
    return min(BACKOFF_MAX_SECONDS, delay + max(0.0, min(jitter(delay), delay / 4)))


def _default_jitter(delay: float) -> float:
    return random.uniform(0.0, delay / 4)  # noqa: S311 - scheduling jitter, not security


class RoomEventUploader:
    """Drains the outbox on one bounded daemon thread. ``deliver_once`` is one attempt and is
    what the tests drive; the thread only loops it with a fixed idle wait."""

    def __init__(
        self,
        outbox: RoomEventOutbox,
        client: EdgeControlPlaneClient,
        *,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[float], float] = _default_jitter,
        poll_seconds: float = POLL_SECONDS,
        join_timeout_seconds: float = 5.0,
    ) -> None:
        self.outbox = outbox
        self._client = client
        self._clock = clock
        self._jitter = jitter
        self._poll = poll_seconds
        self._join_timeout = join_timeout_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def deliver_once(self) -> str:
        """One attempt at the head of the outbox. Returns a bounded outcome word."""
        now = self._clock()
        batch = self.outbox.due(now)
        if not batch:
            return "idle"
        ids = [event_id for event_id, _ in batch]
        try:
            response = self._client.post_json(
                ROOM_TRANSITIONS_PATH, {"events": [payload for _, payload in batch]}
            )
        except ControlPlaneError as exc:
            self.outbox.record(room_transition_upload_failures_total=1)
            self.outbox.note_failure(exc.category)
            if exc.category in CONFIGURATION_CATEGORIES:
                if exc.category == "auth_rejected":
                    self.outbox.record(room_transition_auth_failures_total=1)
                self.outbox.defer(ids, now + BACKOFF_MAX_SECONDS)
                return exc.category
            if exc.category in PERMANENT_CATEGORIES:
                if len(ids) > 1:
                    # Find the bad one: every event of this batch now goes on its own.
                    self.outbox.defer(ids, now, solo=True)
                    return "split"
                self.outbox.dead_letter(ids, exc.category, now)
                return "rejected"
            attempts = self.outbox.attempts(ids[0])
            self.outbox.defer(ids, now + backoff_seconds(attempts, self._jitter))
            return "retry"
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            self.outbox.record(room_transition_upload_failures_total=1)
            self.outbox.note_failure("malformed_response")
            self.outbox.defer(
                ids, now + backoff_seconds(self.outbox.attempts(ids[0]), self._jitter)
            )
            return "retry"
        answers = {
            item.get("event_id"): (item.get("status"), item.get("category"))
            for item in results
            if isinstance(item, dict)
        }
        accepted: list[str] = []
        duplicates: list[str] = []
        missing: list[str] = []
        for event_id in ids:
            status, category = answers.get(event_id, (None, None))
            if status == "ACCEPTED":
                accepted.append(event_id)
            elif status == "DUPLICATE":
                duplicates.append(event_id)
            elif status == "REJECTED":
                self.outbox.dead_letter(
                    [event_id], category if isinstance(category, str) else "rejected", now
                )
            else:
                missing.append(event_id)
        self.outbox.remove(accepted + duplicates)
        self.outbox.record(
            room_transition_events_uploaded_total=len(accepted),
            room_transition_events_duplicate_ack_total=len(duplicates),
        )
        if missing:
            self.outbox.defer(
                missing, now + backoff_seconds(self.outbox.attempts(missing[0]), self._jitter)
            )
        self.outbox.note_failure(None)
        return "delivered"

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="veotrex-room-event-upload", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                outcome = self.deliver_once()
            except Exception:
                # A local storage fault must not end delivery for good; it is retried after the
                # idle wait. The category is fixed text; nothing from the event is logged.
                _logger.warning("room_event_upload_internal_error")
                outcome = "internal_error"
            if outcome not in {"delivered", "split", "rejected"}:
                self._stop.wait(self._poll)

    def stop(self) -> None:
        """Signal and wait briefly; an upload in flight ends within the client timeout. The
        thread is a daemon and anything unacknowledged stays in the outbox for next time."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._join_timeout)
