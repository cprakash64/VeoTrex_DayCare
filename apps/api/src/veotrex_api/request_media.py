"""Media-type negotiation for the provider-posted token-exchange body.

Ring delivers the one-time authorization code from a Java HTTP client that does not send the
single media type this service originally required, so the endpoint accepts a small, explicitly
enumerated set and normalises each one into the canonical JSON body the route model validates.

The set is a closed allowlist, not a relaxation: anything outside it is still refused with 415.
Only the authorization-code field is read; any other field Ring may send is ignored rather than
rejected, so an additive change on Ring's side cannot break linking. The code value is never
logged, echoed, or placed in an exception message - every failure here carries a fixed string.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

JSON_MEDIA_TYPE = "application/json"
FORM_MEDIA_TYPE = "application/x-www-form-urlencoded"
# A body sent with no Content-Type at all. Java's HttpClient omits the header unless the caller
# sets it explicitly, so this is a real transport shape rather than a permissive wildcard.
ABSENT_MEDIA_TYPE = ""

TOKEN_EXCHANGE_MEDIA_TYPES = frozenset({JSON_MEDIA_TYPE, FORM_MEDIA_TYPE, ABSENT_MEDIA_TYPE})

CODE_FIELD = "code"
# Mirrors RingTokenExchangeRequest.code; the route model remains the authority.
MAX_CODE_LENGTH = 2048


class UnsupportedMedia(Exception):
    """The request's media type is outside the endpoint's allowlist."""


class MalformedBody(Exception):
    """The body is unreadable, or carries no single usable authorization code."""


def normalize_media_type(raw: str | None) -> str:
    """Lower-cased media type with parameters stripped. Absent and blank both normalise to ''."""
    if not raw:
        return ABSENT_MEDIA_TYPE
    return raw.split(";", 1)[0].strip().lower()


def _decoded(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise MalformedBody("request body is not valid UTF-8") from None


def _code_from_json(body: bytes) -> str:
    try:
        document = json.loads(_decoded(body))
    except json.JSONDecodeError:
        raise MalformedBody("request body is not valid JSON") from None
    if not isinstance(document, dict):
        raise MalformedBody("request body is not a JSON object")
    value = document.get(CODE_FIELD)
    if not isinstance(value, str):
        raise MalformedBody("authorization code is missing or not a string")
    return value


def _code_from_form(body: bytes) -> str:
    pairs = parse_qsl(_decoded(body), keep_blank_values=True)
    values = [value for name, value in pairs if name == CODE_FIELD]
    if len(values) != 1:
        # Zero is a missing code; more than one is ambiguous and must never be guessed at.
        raise MalformedBody("expected exactly one authorization code field")
    return values[0]


def _code_from_unlabelled(body: bytes) -> str:
    """No Content-Type: choose a parser from the body's own shape, never from a header."""
    if _decoded(body).lstrip().startswith("{"):
        return _code_from_json(body)
    return _code_from_form(body)


def canonical_code_payload(media_type: str, body: bytes) -> bytes:
    """Return the canonical ``{"code": ...}`` JSON body for an accepted media type.

    Raises UnsupportedMedia outside the allowlist, MalformedBody when no single usable code is
    present. The caller is responsible for bounding the body's size before calling this.
    """
    if media_type not in TOKEN_EXCHANGE_MEDIA_TYPES:
        raise UnsupportedMedia("unsupported media type")
    if media_type == JSON_MEDIA_TYPE:
        code = _code_from_json(body)
    elif media_type == FORM_MEDIA_TYPE:
        code = _code_from_form(body)
    else:
        code = _code_from_unlabelled(body)
    if not code:
        raise MalformedBody("authorization code is empty")
    if len(code) > MAX_CODE_LENGTH:
        raise MalformedBody("authorization code exceeds the permitted size")
    return json.dumps({CODE_FIELD: code}, separators=(",", ":")).encode()
