"""Compatibility of the existing Ring integration with the CURRENT official Ring contract.

Evidence re-verified 2026-09-11 from developer.ring.com and
developer.amazon.com/docs/ring/api-documentation.html. These tests pin only values the current
official documentation states; anything Ring leaves undocumented is deliberately not asserted.
"""

import base64
import hashlib
import hmac

import pytest

from veotrex_api.config import Settings
from veotrex_api.ring_nonce import (
    InvalidRingLink,
    compute_ring_nonce,
    ring_nonce_matches,
    validate_ring_timestamp,
)

SYNTHETIC_KEY = "synthetic-hmac-signing-key-not-real"
SYNTHETIC_ACCOUNT = "synthetic-account-id"


def settings() -> Settings:
    return Settings(_env_file=None)


def test_oauth_and_api_hosts_match_current_official_documentation() -> None:
    value = settings()
    # Documented: token endpoint https://oauth.ring.com/oauth/token, API base https://api.amazonvision.com
    assert (
        value.ring_oauth_token_url == "https://oauth.ring.com/oauth/token"  # noqa: S105 - public URL
    )
    assert value.ring_api_base_url == "https://api.amazonvision.com"
    assert value.ring_oauth_token_url.startswith("https://")
    assert value.ring_api_base_url.startswith("https://")


def test_access_token_refresh_margin_fits_documented_four_hour_access_tokens() -> None:
    value = settings()
    # Ring documents ~4 h (14400 s) access tokens and ~30 d refresh tokens that rotate on use.
    assert 30 <= value.ring_access_token_refresh_margin_seconds <= 3_600
    assert value.ring_max_access_token_lifetime_seconds >= 14_400


def test_nonce_matches_the_documented_hmac_construction() -> None:
    timestamp_ms = 1_762_000_000_000
    expected = (
        base64.urlsafe_b64encode(
            hmac.new(
                SYNTHETIC_KEY.encode(),
                f"{timestamp_ms}:{SYNTHETIC_ACCOUNT}".encode(),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    produced = compute_ring_nonce(timestamp_ms, SYNTHETIC_ACCOUNT, SYNTHETIC_KEY)
    assert produced == expected
    assert len(produced) == 43 and "=" not in produced
    assert ring_nonce_matches(produced, timestamp_ms, SYNTHETIC_ACCOUNT, SYNTHETIC_KEY)
    assert not ring_nonce_matches(produced, timestamp_ms + 1, SYNTHETIC_ACCOUNT, SYNTHETIC_KEY)
    assert not ring_nonce_matches("x" * 43, timestamp_ms, SYNTHETIC_ACCOUNT, SYNTHETIC_KEY)


def test_link_validation_window_matches_documented_ten_minutes() -> None:
    value = settings()
    assert value.ring_nonce_validation_window_seconds == 600
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    fresh = int((now - timedelta(seconds=300)).timestamp() * 1000)
    stale = int((now - timedelta(seconds=601)).timestamp() * 1000)
    validate_ring_timestamp(fresh, now=now, validation_window_seconds=600)
    with pytest.raises(InvalidRingLink):
        validate_ring_timestamp(stale, now=now, validation_window_seconds=600)
    with pytest.raises(InvalidRingLink):
        validate_ring_timestamp(int((now + timedelta(seconds=60)).timestamp() * 1000), now=now)


def test_secret_references_never_carry_literal_credentials() -> None:
    value = settings()
    for reference in (value.ring_client_secret_ref, value.ring_hmac_signing_key_ref):
        assert reference.startswith("env:")
        assert len(reference.removeprefix("env:")) <= 64
    assert "secret" not in repr(value).lower() or "ring_client_secret_ref" not in repr(value)
