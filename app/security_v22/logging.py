"""Secret- and PII-safe helpers for structured V2.2 operational logs."""

from __future__ import annotations

import hashlib
import re
from typing import Any


REDACTED = "[REDACTED]"
_MAX_TEXT = 2_000
_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bya29\.[A-Za-z0-9._-]+\b"),
    re.compile(r"\b1//[A-Za-z0-9._-]+\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"https?://[^\s]+", re.IGNORECASE),
    re.compile(
        r"([?&]|\b)(code|state|access_token|refresh_token|id_token|"
        r"code_verifier|client_secret)=([^&\s]*)",
        re.IGNORECASE,
    ),
)


def digest_suffix(value: str, *, length: int = 12) -> str:
    """Return a stable non-reversible suffix for an operational identifier."""

    if not 8 <= length <= 32:
        raise ValueError("digest suffix length is invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def safe_text(value: Any) -> str:
    """Bound and redact token-shaped values without exposing provider bodies."""

    text = str(value)[:_MAX_TEXT]
    for pattern in _PATTERNS:
        if pattern.groups == 3:
            text = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}={REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def safe_error_type(error: BaseException) -> str:
    return type(error).__name__[:120]
