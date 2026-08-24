import hashlib
import hmac
import json

import pytest

from veotrex_api.ring_webhook import RingWebhookError, parse_webhook, verify_signature

KEY = "explicit-test-fixture-key"


def body(**meta_overrides: object) -> bytes:
    meta: dict[str, object] = {
        "version": "1.1",
        "time": "2026-08-23T12:00:00.200155525Z",
        "request_id": "request-1",
        "account_id": "opaque-account",
    }
    meta.update(meta_overrides)
    return json.dumps(
        {
            "meta": meta,
            "data": {
                "id": "event-1",
                "type": "motion_detected",
                "attributes": {
                    "source": "opaque/device",
                    "source_type": "devices",
                    "timestamp": 1787486400000,
                    "sub_type": "human",
                    "component_ids": ["opaque/component"],
                },
                "relationships": {"devices": {"links": {"self": "/v1/devices/opaque"}}},
            },
            "future": {"allowed": True},
        },
        separators=(",", ":"),
    ).encode()


def signature(value: bytes, key: str = KEY) -> str:
    return "sha256=" + hmac.new(key.encode(), value, hashlib.sha256).hexdigest()


def test_signature_uses_exact_raw_body() -> None:
    raw = body()
    assert verify_signature(raw, signature(raw), KEY)
    assert not verify_signature(raw + b" ", signature(raw), KEY)
    assert not verify_signature(raw, signature(raw, "wrong-key"), KEY)
    assert not verify_signature(raw, None, KEY)
    assert not verify_signature(raw, "SHA256=" + "0" * 64, KEY)
    assert not verify_signature(raw, "sha256=" + "0" * 63, KEY)
    assert not verify_signature(raw, "sha256=" + "g" * 64, KEY)


def test_envelope_is_explicit_and_forward_compatible() -> None:
    value = parse_webhook(body())
    assert value.account_id == "opaque-account"
    assert value.source_id == "opaque/device"
    assert value.component_ids == ("opaque/component",)
    assert value.event_type == "motion_detected"


def test_unknown_event_is_valid_but_unknown_version_is_not_reinterpreted() -> None:
    payload = json.loads(body())
    payload["data"]["type"] = "future_event"
    assert parse_webhook(json.dumps(payload).encode()).event_type == "future_event"
    with pytest.raises(RingWebhookError, match="unsupported_version"):
        parse_webhook(body(version="2.0"))


@pytest.mark.parametrize("value", [b"", b"[]", b'{"meta":{}}'])
def test_malformed_envelope_fails_closed(value: bytes) -> None:
    with pytest.raises(RingWebhookError, match="malformed_envelope"):
        parse_webhook(value)
