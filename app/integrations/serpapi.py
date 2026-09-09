"""Secret-safe SerpAPI key rotation shared by v2.1 and v2.2."""

from __future__ import annotations

import hashlib
import inspect
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class HttpClient(Protocol):
    async def get(self, url: str, **kwargs: Any) -> Any: ...


BeforeAttempt = Callable[[int, str], Awaitable[None] | None]
KeyAvailable = Callable[[str], Awaitable[bool] | bool]
AfterAttempt = Callable[[int, str, str | None], Awaitable[None] | None]


class SerpApiKeysUnavailable(RuntimeError):
    """Raised when no configured SerpAPI key can execute the request."""


class SerpApiTransportError(RuntimeError):
    """Raised for a secret-safe network failure."""


class SerpApiInvalidResponse(RuntimeError):
    """Raised when SerpAPI does not return JSON."""


class SerpApiHttpError(RuntimeError):
    """Raised for non-key-related HTTP failures."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"SerpAPI request failed with HTTP {status_code}.")
        self.status_code = status_code


@dataclass
class SerpApiKeyState:
    active_fingerprint: str = ""
    blocked_until: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SerpApiCallMetadata:
    attempt_count: int
    key_slot: int
    key_fingerprint: str


@dataclass(frozen=True)
class SerpApiResponse:
    payload: dict[str, Any]
    metadata: SerpApiCallMetadata


def configured_serpapi_keys(*values: str | None) -> list[str]:
    keys = [str(value or "").strip() for value in values]
    return list(dict.fromkeys(key for key in keys if key))


def serpapi_key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _normalize_message(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def serpapi_payload_error(data: dict[str, Any]) -> str | None:
    error = data.get("error")
    if error:
        return str(error)
    metadata = data.get("search_metadata")
    if isinstance(metadata, dict):
        status = str(metadata.get("status") or "").strip().lower()
        if status and status not in {"success", "cached"}:
            return f"SerpAPI search status was {metadata.get('status')}."
    return None


def serpapi_key_failure_kind(status_code: int, message: str | None) -> str | None:
    normalized = _normalize_message(message)
    if status_code in {401, 403} or any(
        marker in normalized
        for marker in (
            "invalid api key",
            "no valid api key",
            "account has been deleted",
            "account is disabled",
            "doesn t have permission",
        )
    ):
        return "invalid_or_forbidden"
    if "run out of searches" in normalized or "no searches remaining" in normalized:
        return "monthly_quota_exhausted"
    if status_code == 429:
        return "rate_limited"
    return None


async def _notify_before_attempt(
    callback: BeforeAttempt | None,
    slot: int,
    fingerprint: str,
) -> None:
    if callback is None:
        return
    result = callback(slot, fingerprint)
    if inspect.isawaitable(result):
        await result


async def _key_is_available(callback: KeyAvailable | None, fingerprint: str) -> bool:
    if callback is None:
        return True
    result = callback(fingerprint)
    return bool(await result) if inspect.isawaitable(result) else bool(result)


async def _notify_after_attempt(
    callback: AfterAttempt | None,
    slot: int,
    fingerprint: str,
    failure_kind: str | None,
) -> None:
    if callback is None:
        return
    result = callback(slot, fingerprint, failure_kind)
    if inspect.isawaitable(result):
        await result


async def execute_serpapi_get(
    client: HttpClient,
    params: dict[str, str],
    *,
    keys: Sequence[str],
    base_url: str,
    state: SerpApiKeyState | None = None,
    before_attempt: BeforeAttempt | None = None,
    key_available: KeyAvailable | None = None,
    after_attempt: AfterAttempt | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    logger: logging.Logger | None = None,
) -> SerpApiResponse:
    """Execute one logical request with bounded, secret-safe key failover."""
    configured = configured_serpapi_keys(*keys)
    if not configured:
        raise SerpApiKeysUnavailable("No SerpAPI key is configured.")

    key_state = state or SerpApiKeyState()
    now = monotonic()
    ordered = sorted(
        enumerate(configured),
        key=lambda item: (
            serpapi_key_fingerprint(item[1]) != key_state.active_fingerprint,
            item[0],
        ),
    )
    available = []
    for item in ordered:
        fingerprint = serpapi_key_fingerprint(item[1])
        if key_state.blocked_until.get(fingerprint, 0) <= now and await _key_is_available(key_available, fingerprint):
            available.append(item)
    if not available:
        raise SerpApiKeysUnavailable(
            "All configured SerpAPI keys are temporarily unavailable."
        )

    safe_logger = logger or logging.getLogger(__name__)
    last_kind = "unavailable"
    attempt_count = 0
    for slot, key in available:
        fingerprint = serpapi_key_fingerprint(key)
        await _notify_before_attempt(before_attempt, slot + 1, fingerprint)
        attempt_count += 1
        try:
            response = await client.get(base_url, params={**params, "api_key": key})
        except Exception as exc:  # noqa: BLE001
            await _notify_after_attempt(after_attempt, slot + 1, fingerprint, "transport")
            raise SerpApiTransportError("SerpAPI transport request failed.") from exc

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            await _notify_after_attempt(after_attempt, slot + 1, fingerprint, "invalid_response")
            raise SerpApiInvalidResponse("SerpAPI returned an invalid JSON response.") from exc
        data = payload if isinstance(payload, dict) else {}
        status_code = int(getattr(response, "status_code", 200) or 200)
        failure_kind = serpapi_key_failure_kind(
            status_code,
            serpapi_payload_error(data),
        )
        if failure_kind:
            await _notify_after_attempt(after_attempt, slot + 1, fingerprint, failure_kind)
            last_kind = failure_kind
            cooldown = 60.0 if failure_kind == "rate_limited" else 300.0
            key_state.blocked_until[fingerprint] = monotonic() + cooldown
            if key_state.active_fingerprint == fingerprint:
                key_state.active_fingerprint = ""
            safe_logger.warning(
                "[SerpAPI] key slot %d unavailable reason=%s; trying next configured key",
                slot + 1,
                failure_kind,
            )
            continue

        if status_code >= 400:
            await _notify_after_attempt(after_attempt, slot + 1, fingerprint, f"http_{status_code}")
            raise SerpApiHttpError(status_code)

        if key_state.active_fingerprint != fingerprint:
            safe_logger.info("[SerpAPI] using configured key slot %d", slot + 1)
        key_state.active_fingerprint = fingerprint
        await _notify_after_attempt(after_attempt, slot + 1, fingerprint, None)
        return SerpApiResponse(
            payload=data,
            metadata=SerpApiCallMetadata(
                attempt_count=attempt_count,
                key_slot=slot + 1,
                key_fingerprint=fingerprint,
            ),
        )

    raise SerpApiKeysUnavailable(
        f"All configured SerpAPI keys are unavailable ({last_kind})."
    )
