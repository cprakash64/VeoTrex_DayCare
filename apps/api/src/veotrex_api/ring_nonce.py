import base64
import hashlib
import hmac
import re
from datetime import UTC, datetime

NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class InvalidRingLink(Exception):
    pass


def validate_ring_timestamp(
    timestamp_ms: int,
    *,
    now: datetime | None = None,
    validation_window_seconds: int = 600,
    future_tolerance_seconds: int = 0,
) -> None:
    resolved_now = now or datetime.now(UTC)
    now_ms = int(resolved_now.timestamp() * 1000)
    age_ms = now_ms - timestamp_ms
    if age_ms > validation_window_seconds * 1000:
        raise InvalidRingLink("link request expired")
    if age_ms < -(future_tolerance_seconds * 1000):
        raise InvalidRingLink("link timestamp is in the future")


def compute_ring_nonce(timestamp_ms: int, account_id: str, signing_key: str) -> str:
    payload = f"{timestamp_ms}:{account_id}".encode()
    digest = hmac.new(signing_key.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def ring_nonce_matches(
    received_nonce: str, timestamp_ms: int, account_id: str, signing_key: str
) -> bool:
    if NONCE_PATTERN.fullmatch(received_nonce) is None:
        return False
    expected = compute_ring_nonce(timestamp_ms, account_id, signing_key)
    return hmac.compare_digest(expected, received_nonce)
