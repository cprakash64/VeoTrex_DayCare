from datetime import UTC, datetime, timedelta

import pytest

from veotrex_api.ring_nonce import (
    InvalidRingLink,
    compute_ring_nonce,
    ring_nonce_matches,
    validate_ring_timestamp,
)


def test_ring_nonce_official_shape_and_deterministic_vector() -> None:
    value = compute_ring_nonce(1750000000123, "ring-account-42", "test-signing-key")
    assert value == "_GWJsx9xa8catDWkSN0OPFbBVXPNc5H7Xx8BVgfsC9w"
    assert len(value) == 43
    assert "=" not in value
    assert ring_nonce_matches(value, 1750000000123, "ring-account-42", "test-signing-key")
    assert not ring_nonce_matches(value, 1750000000123, "other", "test-signing-key")
    assert not ring_nonce_matches(value, 1750000000124, "ring-account-42", "test-signing-key")
    assert not ring_nonce_matches(
        value[:-1] + "A", 1750000000123, "ring-account-42", "test-signing-key"
    )
    assert not ring_nonce_matches(value + "=", 1750000000123, "ring-account-42", "test-signing-key")


def test_ring_timestamp_is_strictly_bounded() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    validate_ring_timestamp(int(now.timestamp() * 1000), now=now)
    validate_ring_timestamp(int((now - timedelta(seconds=600)).timestamp() * 1000), now=now)
    with pytest.raises(InvalidRingLink, match="expired"):
        validate_ring_timestamp(
            int((now - timedelta(seconds=600, milliseconds=1)).timestamp() * 1000), now=now
        )
    with pytest.raises(InvalidRingLink, match="future"):
        validate_ring_timestamp(int((now + timedelta(milliseconds=1)).timestamp() * 1000), now=now)


def test_nonce_comparison_uses_constant_time_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def compare(first: str, second: str) -> bool:
        calls.append((first, second))
        return first == second

    monkeypatch.setattr("veotrex_api.ring_nonce.hmac.compare_digest", compare)
    value = compute_ring_nonce(1750000000123, "ring-account-42", "test-signing-key")
    assert ring_nonce_matches(value, 1750000000123, "ring-account-42", "test-signing-key")
    assert calls == [(value, value)]
