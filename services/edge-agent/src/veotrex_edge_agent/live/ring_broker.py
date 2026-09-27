"""The brokered WHEP session provider behind ``RingWhepSource`` (V1-DEMO-03C).

``RingWhepSource`` asks a ``RingLiveSessionProvider`` for a session and later releases it; it has
never known how a session is authorized, and it still does not. This provider is the only live
module that touches the broker path, and even here nothing is built by hand: the session lease
(control-plane origin, camera UUID, machine credential read from its protected file) comes from
``BrokeredWhepSessionProvider``, and the offer/answer exchange and lease DELETE come from
``BrokeredWhepExchange`` over the existing ``WhepClient``.

What the source holds is ``RingSessionMaterial``: an opaque random id, the non-secret broker path,
and a one-shot ``negotiate`` capability. The reader calls that with the WebRTC worker's offer and
gets the answer back. No bearer value, lease URL or Ring identifier is a field of anything the
source or the reader holds, and a Ring OAuth token does not exist on this machine at all.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from uuid import UUID

from veotrex_edge_agent.camera_transport.broker_whep import (
    BrokeredWhepExchange,
    BrokeredWhepSessionProvider,
)
from veotrex_edge_agent.camera_transport.descriptor import LiveSessionLease
from veotrex_edge_agent.camera_transport.errors import TransportError
from veotrex_edge_agent.live.ring_media import RingMediaError, RingSessionMaterial

# Sessions a provider may hold at once. RingWhepSource holds one; the bound only exists so a
# caller that forgot to release cannot grow this without limit.
MAX_OPEN_SESSIONS = 4


@dataclass(slots=True, repr=False)
class _Session:
    generation: int
    lease: LiveSessionLease
    negotiated: bool = False


class BrokeredRingSessionProvider:
    """``RingLiveSessionProvider`` whose sessions are negotiated through the VeoTrex broker."""

    def __init__(
        self,
        camera_id: UUID,
        sessions: BrokeredWhepSessionProvider,
        exchange: BrokeredWhepExchange,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._camera_id = camera_id
        self._sessions = sessions
        self._exchange = exchange
        self._clock = clock
        self._lock = threading.Lock()
        self._open: dict[str, _Session] = {}
        self._generation = 0

    @property
    def open_sessions(self) -> int:
        with self._lock:
            return len(self._open)

    def acquire(self) -> RingSessionMaterial:
        """A fresh session ticket. Fails closed before any network I/O: WebRTC runtime first,
        then the credential file, both inside ``BrokeredWhepSessionProvider``."""
        with self._lock:
            if len(self._open) >= MAX_OPEN_SESSIONS:
                raise RingMediaError("session_limit_reached")
            self._generation += 1
            generation = self._generation
        try:
            lease = self._sessions.lease(self._camera_id, generation, self._clock())
        except TransportError as exc:
            raise RingMediaError(exc.category.value) from None
        session_id = secrets.token_hex(16)
        with self._lock:
            self._open[session_id] = _Session(generation, lease)
        return RingSessionMaterial(
            session_id=session_id,
            resource_path=f"/v1/edge/cameras/{self._camera_id}/whep",
            codec="H264",
            negotiate=partial(self._negotiate, session_id),
        )

    def _negotiate(self, session_id: str, offer_sdp: str) -> str:
        with self._lock:
            session = self._open.get(session_id)
            if session is None or session.negotiated:
                # Released already, or a second offer for one session: never re-POST.
                raise RingMediaError("invalid_session_material")
            session.negotiated = True
        try:
            return self._exchange(offer_sdp, session.lease)
        except TransportError as exc:
            raise RingMediaError(exc.category.value) from None

    def release(self, material: RingSessionMaterial) -> None:
        """DELETE the broker lease, if one was created. Idempotent; never raises."""
        with self._lock:
            session = self._open.pop(material.session_id, None)
        if session is not None and session.negotiated:
            self._exchange.release(session.generation)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._open.values())
            self._open.clear()
        for session in sessions:
            if session.negotiated:
                self._exchange.release(session.generation)

    def __repr__(self) -> str:
        return (
            f"BrokeredRingSessionProvider(camera_id={self._camera_id}, "
            f"open_sessions={self.open_sessions}, credential=REDACTED)"
        )
