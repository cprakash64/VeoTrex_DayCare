"""V1-DEMO-03B: the process-local WHEP lease registry is bounded, owned and expiring."""

from __future__ import annotations

import re
import threading
from uuid import uuid4

import pytest

from veotrex_api.edge_whep import (
    EdgeBrokerError,
    ReleaseOutcome,
    WhepLeaseRegistry,
    validate_offer,
)

TENANT, NODE, OTHER_NODE = uuid4(), uuid4(), uuid4()


def registry(**kwargs: float) -> tuple[WhepLeaseRegistry, list[float]]:
    now = [100.0]
    values = {"max_active": 4, "max_per_node": 2, "ttl_seconds": 60.0, **kwargs}
    return (
        WhepLeaseRegistry(
            max_active=int(values["max_active"]),
            max_per_node=int(values["max_per_node"]),
            ttl_seconds=values["ttl_seconds"],
            clock=lambda: now[0],
        ),
        now,
    )


def open_lease(subject: WhepLeaseRegistry, node=NODE, url: str | None = "https://ring/x"):  # type: ignore[no-untyped-def]
    reservation = subject.reserve(TENANT, node)
    assert reservation is not None
    return subject.commit(
        reservation, camera_id=uuid4(), connection_id=uuid4(), provider_session_url=url
    )


def test_bounds_are_validated() -> None:
    for kwargs in ({"max_per_node": 5}, {"max_per_node": 0}, {"ttl_seconds": 0}):
        with pytest.raises(ValueError):
            registry(**kwargs)


def test_per_node_and_global_bounds_include_in_flight_reservations() -> None:
    subject, _ = registry(max_active=3, max_per_node=2)
    first = subject.reserve(TENANT, NODE)
    second = subject.reserve(TENANT, NODE)
    assert first and second and subject.reserve(TENANT, NODE) is None
    third = subject.reserve(TENANT, OTHER_NODE)
    assert third is not None
    assert subject.reserve(TENANT, uuid4()) is None, "global bound reached"
    subject.cancel(first)
    subject.cancel(first)  # idempotent
    assert subject.pending_count == 2
    assert subject.reserve(TENANT, NODE) is not None


def test_commit_issues_an_opaque_unguessable_lease() -> None:
    subject, now = registry()
    lease = open_lease(subject)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", lease.lease_id)
    assert lease.location == f"/v1/edge/whep-leases/{lease.lease_id}"
    assert lease.expires_monotonic == now[0] + 60
    for rendered in (repr(lease), str(lease)):
        assert lease.lease_id not in rendered and "ring" not in rendered
    assert open_lease(subject).lease_id != lease.lease_id
    reservation = subject.reserve(TENANT, NODE)
    assert reservation is None, "two leases already held by NODE"


def test_only_the_owner_releases_and_repeats_are_idempotent() -> None:
    subject, now = registry()
    lease = open_lease(subject)
    assert subject.release_owned(lease.lease_id, TENANT, OTHER_NODE) == (
        ReleaseOutcome.NOT_FOUND,
        None,
    )
    assert subject.release_owned(lease.lease_id, uuid4(), NODE)[0] is ReleaseOutcome.NOT_FOUND
    assert subject.active_count == 1
    outcome, released = subject.release_owned(lease.lease_id, TENANT, NODE)
    assert outcome is ReleaseOutcome.RELEASED and released is lease
    assert subject.release_owned(lease.lease_id, TENANT, NODE)[0] is ReleaseOutcome.ALREADY_RELEASED
    assert subject.release_owned(lease.lease_id, TENANT, OTHER_NODE)[0] is ReleaseOutcome.NOT_FOUND
    now[0] += 61
    subject.pop_expired()
    assert subject.release_owned(lease.lease_id, TENANT, NODE)[0] is ReleaseOutcome.NOT_FOUND
    assert subject.release_owned("unknown", TENANT, NODE)[0] is ReleaseOutcome.NOT_FOUND


def test_expiry_releases_exactly_the_stale_leases_and_frees_capacity() -> None:
    subject, now = registry(max_active=2, max_per_node=2)
    old = open_lease(subject)
    now[0] += 30
    young = open_lease(subject)
    assert subject.reserve(TENANT, NODE) is None
    now[0] += 31
    assert subject.pop_expired() == [old]
    assert subject.active_count == 1
    assert subject.reserve(TENANT, NODE) is not None
    now[0] += 60
    assert subject.pop_expired() == [young]


def test_drain_empties_the_registry_and_resets_counts() -> None:
    subject, _ = registry(max_active=2, max_per_node=2)
    leases = [open_lease(subject), open_lease(subject)]
    assert sorted(lease.lease_id for lease in subject.drain()) == sorted(
        lease.lease_id for lease in leases
    )
    assert subject.active_count == 0 and subject.drain() == []
    assert subject.reserve(TENANT, NODE) is not None


def test_released_ids_are_remembered_in_bounded_memory() -> None:
    subject, _ = registry(max_active=1, max_per_node=1)
    for _ in range(50):
        lease = open_lease(subject)
        subject.release_owned(lease.lease_id, TENANT, NODE)
    assert len(subject._tombstones) <= 8


def test_concurrent_reservations_never_exceed_the_bound() -> None:
    subject, _ = registry(max_active=16, max_per_node=16)
    granted: list[object] = []
    lock = threading.Lock()
    barrier = threading.Barrier(32)

    def worker() -> None:
        barrier.wait()
        for _ in range(50):
            reservation = subject.reserve(TENANT, NODE)
            if reservation is not None:
                with lock:
                    granted.append(reservation)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(granted) == 16
    assert subject.pending_count == 16


@pytest.mark.parametrize(
    "offer",
    [
        b"",
        b"v=0\r\n",
        b"v=1\r\nm=video 9 RTP 96\r\n",
        b"v=0\r\nm=video 9 RTP 96\r\nX=upper\r\n",
        b"v=0\r\nm=video 9 RTP 96\r\na=x\x00y\r\n",
        b"\xff\xfe",
        b"v=0\r\nm=video 9 RTP 96\r\n" + b"a=x\r\n" * 5000,
    ],
)
def test_offers_are_validated(offer: bytes) -> None:
    with pytest.raises(EdgeBrokerError) as caught:
        validate_offer(offer, 1_000_000)
    assert caught.value.status_code == 400


def test_a_valid_offer_is_returned_unchanged() -> None:
    offer = b"v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    assert validate_offer(offer, 1024) == offer
    with pytest.raises(EdgeBrokerError):
        validate_offer(offer, 10)
