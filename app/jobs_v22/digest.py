"""Deterministic JSON encoding for v2.2 request identity."""

from __future__ import annotations

import hashlib
from typing import Any

import orjson
from pydantic import BaseModel


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON-compatible data with stable object-key ordering."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def request_digest(value: Any) -> str:
    """Return the stable digest stored with an idempotent task."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"
