"""Deterministic JSON encoding for v2.2 request identity."""

from __future__ import annotations

import hashlib
import json
import math
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


def _verified_number(value: int | float) -> str:
    """ECMAScript Number rendering; repr supplies shortest round-trip digits.

    All JSON numbers have binary64 semantics, including integers parsed by Python.
    The fixed/exponent cutovers follow RFC 8785 / ECMAScript JSON.stringify.
    """
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError("Verified JSON numbers must be finite binary64 values") from error
    if not math.isfinite(number):
        raise ValueError("Verified JSON numbers must be finite binary64 values")
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    mantissa, _, exponent = repr(magnitude).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).rstrip("0")
    point = len(whole) + (int(exponent) if exponent else 0)
    if 1e-6 <= magnitude < 1e21:
        if point <= 0:
            rendered = "0." + "0" * -point + digits
        elif point >= len(digits):
            rendered = digits + "0" * (point - len(digits))
        else:
            rendered = digits[:point] + "." + digits[point:]
    else:
        rendered = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        power = point - 1
        rendered += "e" + ("+" if power >= 0 else "-") + str(abs(power))
    return sign + rendered


def verified_canonical_json_bytes(value: Any) -> bytes:
    """Verified boundary only: match frontend sorted keys + JSON.stringify.

    Do not replace canonical_json_bytes: existing checkpoint and snapshot IDs
    deliberately retain their original orjson encoding. Hash the raw loaded
    parent JSON, not a model dump that may insert defaults or coerce its fields.
    """
    ancestors: set[int] = set()

    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if type(item) is bool:
            return "true" if item else "false"
        if type(item) in (int, float):
            return _verified_number(item)
        if type(item) is str:
            return json.dumps(item, ensure_ascii=False)
        if type(item) not in (list, dict):
            raise TypeError("Verified digests require plain JSON values")
        identity = id(item)
        if identity in ancestors:
            raise ValueError("Verified digests require acyclic JSON values")
        ancestors.add(identity)
        try:
            if type(item) is list:
                return "[" + ",".join(encode(child) for child in item) + "]"
            if any(type(key) is not str for key in item):
                raise TypeError("Verified JSON object keys must be strings")
            keys = sorted(item, key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(encode(key) + ":" + encode(item[key]) for key in keys) + "}"
        finally:
            ancestors.remove(identity)

    return encode(value).encode("utf-8")


def verified_request_digest(value: Any) -> str:
    """Hash a Verified parent payload or stable request binding, not checkpoints."""
    return f"sha256:{hashlib.sha256(verified_canonical_json_bytes(value)).hexdigest()}"
