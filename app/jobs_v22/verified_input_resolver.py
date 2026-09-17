"""Bounded service-role RPC boundary for frozen Verified analysis inputs."""
from __future__ import annotations

import json
import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID
from weakref import WeakKeyDictionary

import httpx

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.digest import request_digest, verified_request_digest
from app.jobs_v22.verified_models import VerifiedResolvedInput, VerifiedTaskRequest

MAX_RESPONSE_BYTES = 25_000_000
TOTAL_TIMEOUT_SECONDS = 20
REJECTION_DIAGNOSTIC_BYTES = 8_192
logger = logging.getLogger(__name__)

_SAFE_REJECTION_REASONS = frozenset({
    "immutable v2.2 public GBP reference conflict",
    "immutable v2.2 report conflict",
    "immutable v2.2 source snapshot conflict",
    "invalid v2.2 result checksum",
    "prospect analysis generation is no longer active",
    "prospect analysis job is not persistable",
    "v2.2 prospect report identity mismatch",
    "v2.2 public GBP report binding mismatch",
    "v2.2 public GBP source mismatch",
    "v2.2 report could not be linked to its job",
    "v2.2 report references an unknown snapshot",
    "v2.2 result payloads must be objects",
    "v2.2 result snapshot identities must be unique",
    "v2.2 source snapshot lineage mismatch",
    "v2.2 source snapshot schema mismatch",
})


async def _safe_rejection_reason(response: httpx.Response) -> str | None:
    """Return only an allowlisted database invariant; never log raw provider text."""

    raw = bytearray()
    chunks = (response.content,) if response.is_stream_consumed else response.aiter_raw()
    async for chunk in _async_chunks(chunks):
        if len(raw) + len(chunk) > REJECTION_DIAGNOSTIC_BYTES:
            return None
        raw.extend(chunk)
    try:
        message = json.loads(raw).get("message")
    except (AttributeError, TypeError, ValueError, RecursionError):
        return None
    return message if message in _SAFE_REJECTION_REASONS else None


@dataclass(frozen=True, eq=False, slots=True, weakref_slot=True)
class TrustedVerifiedInput:
    """Resolver capability; only the exact registered instance may be persisted.

    Read the validated transport model through ``payload`` or its forwarded
    properties. Dumping/copying/revalidating the payload never transfers trust;
    the expected parent digest is held separately in the resolver's registry.
    """

    payload: VerifiedResolvedInput

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "payload"), name)


@dataclass(frozen=True)
class _TrustedInputSeal:
    graph_digest: str
    raw_parent_verified_digest: str
    run_generation: int


_TRUSTED_INPUT_SEALS: WeakKeyDictionary[TrustedVerifiedInput, _TrustedInputSeal] = WeakKeyDictionary()


def require_trusted_verified_input(
    value: TrustedVerifiedInput, *, run_generation: int
) -> VerifiedResolvedInput:
    if type(value) is not TrustedVerifiedInput:
        raise ValueError("input was not returned by the trusted resolver")
    seal = _TRUSTED_INPUT_SEALS.get(value)
    if (seal is None or seal.run_generation != run_generation
            or request_digest(value.payload) != seal.graph_digest
            or value.parent_payload_checksum != seal.raw_parent_verified_digest):
        raise ValueError("input provenance is missing or the resolved graph was mutated")
    return value.payload


class VerifiedRpcClient:
    """Shared bounded transport; never expose provider body or exception context."""

    def __init__(self, *, url: str, service_role_key: str, http_client: httpx.AsyncClient,
                 max_response_bytes: int, prefix: str, invalid_retryable: bool = False):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/") or not service_role_key):
            raise DeterministicJobError(prefix + "_NOT_CONFIGURED", "Verified report storage is not configured.")
        self.url = url.rstrip("/")
        self.key = service_role_key
        self.client = http_client
        self.limit = max_response_bytes
        self.prefix = prefix
        self.invalid_retryable = invalid_retryable

    def invalid(self):
        error = TransientJobError if self.invalid_retryable else DeterministicJobError
        return error(self.prefix + "_INVALID_RESPONSE", "Verified report storage returned an invalid response.")

    async def post(self, rpc: str, payload: dict):
        try:
            # HTTPX read timeouts measure inactivity; this bounds the entire RPC,
            # including connection/header delays and a continuously trickled body.
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                async with self.client.stream("POST", f"{self.url}/rest/v1/rpc/{rpc}",
                    headers={"apikey": self.key, "authorization": f"Bearer {self.key}",
                        "content-type": "application/json", "accept-encoding": "identity"},
                    json=payload, timeout=20, follow_redirects=False) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise TransientJobError(self.prefix + "_UNAVAILABLE", "Verified report storage is temporarily unavailable.") from None
                    if not 200 <= response.status_code < 300:
                        reason = await _safe_rejection_reason(response)
                        if reason is not None:
                            logger.warning("verified RPC rejected prefix=%s rpc=%s status=%d reason=%s",
                                self.prefix, rpc, response.status_code, reason)
                        raise DeterministicJobError(self.prefix + "_REJECTED", "The Verified report request could not be completed safely.") from None
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        raise self.invalid() from None
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isascii() or not length.isdecimal() or len(length) > 10
                            or int(length) > self.limit):
                        raise self.invalid() from None
                    raw = bytearray()
                    # Never instantiate a decoder: compressed payloads were rejected
                    # above, so these are also the bounded uncompressed JSON bytes.
                    if response.is_stream_consumed:
                        chunks = (response.content,)
                    else:
                        chunks = response.aiter_raw()
                    async for chunk in _async_chunks(chunks):
                        if len(raw) + len(chunk) > self.limit:
                            raise self.invalid() from None
                        raw.extend(chunk)
        except (TimeoutError, httpx.TimeoutException, httpx.NetworkError):
            raise TransientJobError(self.prefix + "_UNAVAILABLE", "Verified report storage is temporarily unavailable.") from None
        except (httpx.DecodingError, httpx.RemoteProtocolError):
            raise self.invalid() from None
        try:
            return json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            raise self.invalid() from None


async def _async_chunks(chunks):
    if hasattr(chunks, "__aiter__"):
        async for chunk in chunks:
            yield chunk
    else:
        for chunk in chunks:
            yield chunk


class SupabaseVerifiedInputResolver:
    def __init__(self, *, url: str, service_role_key: str, http_client: httpx.AsyncClient):
        self.rpc = VerifiedRpcClient(url=url, service_role_key=service_role_key, http_client=http_client,
            max_response_bytes=MAX_RESPONSE_BYTES, prefix="V22_VERIFIED_INPUT")

    async def resolve(self, *, job_id: UUID, request: VerifiedTaskRequest, run_generation: int) -> TrustedVerifiedInput:
        if type(run_generation) is not int or run_generation < 1:
            raise DeterministicJobError("V22_VERIFIED_INPUT_INVALID", "Invalid Verified task generation.") from None
        raw = await self.rpc.post("resolve_v22_verified_analysis_input", {
            "p_job_id": str(job_id), "p_case_id": str(request.case_id), "p_run_generation": run_generation})
        try:
            # Preserve raw JSON number/string/default semantics for the frontend hash.
            if (not isinstance(raw, dict) or not isinstance(raw.get("parent_report"), dict)
                    or verified_request_digest(raw["parent_report"]) != raw.get("parent_payload_checksum")):
                raise ValueError("parent payload checksum mismatch")
            result = VerifiedResolvedInput.model_validate_json(json.dumps(raw))
            result.validate_request(job_id=job_id, request=request)
            trusted = TrustedVerifiedInput(result)
            # This is the only registration site: the raw frontend checksum and
            # complete payload graph have succeeded. No model constructor, dump,
            # copy, private attribute or post-init hook can re-establish trust.
            _TRUSTED_INPUT_SEALS[trusted] = _TrustedInputSeal(
                graph_digest=request_digest(result),
                raw_parent_verified_digest=result.parent_payload_checksum,
                run_generation=run_generation,
            )
            return trusted
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise DeterministicJobError("V22_VERIFIED_INPUT_INVALID", "Verified report storage returned inconsistent inputs.") from None
