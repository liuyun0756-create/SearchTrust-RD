"""Bounded service-role RPC boundary for frozen Verified analysis inputs."""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.verified_models import VerifiedResolvedInput, VerifiedTaskRequest

MAX_RESPONSE_BYTES = 25_000_000


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
            async with self.client.stream("POST", f"{self.url}/rest/v1/rpc/{rpc}",
                headers={"apikey": self.key, "authorization": f"Bearer {self.key}", "content-type": "application/json"},
                json=payload, timeout=20, follow_redirects=False) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise TransientJobError(self.prefix + "_UNAVAILABLE", "Verified report storage is temporarily unavailable.") from None
                if not 200 <= response.status_code < 300:
                    raise DeterministicJobError(self.prefix + "_REJECTED", "The Verified report request could not be completed safely.") from None
                length = response.headers.get("content-length")
                if length is not None and (not length.isascii() or not length.isdecimal() or len(length) > 10
                        or int(length) > self.limit):
                    raise self.invalid() from None
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > self.limit:
                        raise self.invalid() from None
                    raw.extend(chunk)
        except (httpx.TimeoutException, httpx.NetworkError):
            raise TransientJobError(self.prefix + "_UNAVAILABLE", "Verified report storage is temporarily unavailable.") from None
        except (httpx.DecodingError, httpx.RemoteProtocolError):
            raise self.invalid() from None
        try:
            return json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            raise self.invalid() from None


class SupabaseVerifiedInputResolver:
    def __init__(self, *, url: str, service_role_key: str, http_client: httpx.AsyncClient):
        self.rpc = VerifiedRpcClient(url=url, service_role_key=service_role_key, http_client=http_client,
            max_response_bytes=MAX_RESPONSE_BYTES, prefix="V22_VERIFIED_INPUT")

    async def resolve(self, *, job_id: UUID, request: VerifiedTaskRequest, run_generation: int) -> VerifiedResolvedInput:
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
            return result
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise DeterministicJobError("V22_VERIFIED_INPUT_INVALID", "Verified report storage returned inconsistent inputs.") from None
