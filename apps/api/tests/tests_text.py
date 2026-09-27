"""Text helpers for the "no PII / no biometric word in this output" assertions.

Those checks scan rendered audit metadata or response bodies for words such as "face". The
same text carries random UUIDs (and SHA-256 revisions), and hex can spell "face": a UUID
ending in ``...fbcface`` failed CI once. Identifiers are therefore replaced before the scan.
Only canonical UUIDs and ``sha256:`` digests are replaced, so a forbidden word anywhere else
in the text is still found.
"""

from __future__ import annotations

import re

_IDENTIFIERS = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|sha256:[0-9a-f]{64}",
    re.IGNORECASE,
)


def without_identifiers(text: str) -> str:
    """``text`` with every UUID and SHA-256 revision replaced by ``<id>``."""
    return _IDENTIFIERS.sub("<id>", text)
