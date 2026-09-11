"""V2.2-only Dify adapter for choosing bounded backend-owned copy patterns."""

from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from typing import Any
from uuid import UUID

import httpx

from app.jobs_v22.circuit_breaker import RedisCircuitBreaker
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.copy_models import CopyRequestV1


class DifyControlledCopyProvider:
    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        model_version: str,
        http_client: httpx.AsyncClient,
        circuit_breaker: RedisCircuitBreaker | None = None,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.model_version = model_version
        self.http_client = http_client
        self.circuit_breaker = circuit_breaker
        self.max_response_bytes = max_response_bytes

    async def generate(
        self,
        *,
        job_id: UUID,
        request: CopyRequestV1,
        cost_ledger: JobCostLedger | None = None,
    ) -> object:
        if not self.api_key:
            raise DeterministicJobError(
                "V22_COPY_PROVIDER_NOT_CONFIGURED",
                "The v2.2 copy service is not configured.",
            )
        permit = None
        if self.circuit_breaker is not None:
            permit = await self.circuit_breaker.before_call(
                "dify", "controlled_copy", now=datetime.now(timezone.utc)
            )
        claim = await cost_ledger.claim("dify_workflow") if cost_ledger is not None else None
        started = monotonic()
        try:
            response = await self.http_client.post(
                f"{self.api_url}/workflows/run",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "inputs": {"copy_request": request.model_dump_json()},
                    "response_mode": "blocking",
                    "user": f"v22-job-{job_id}",
                },
            )
        except httpx.HTTPError as exc:
            if claim is not None:
                await cost_ledger.complete(
                    claim.claim_id,
                    outcome="failure",
                    duration_ms=_elapsed_ms(started),
                )
            if permit is not None:
                await self.circuit_breaker.record_failure(permit, now=datetime.now(timezone.utc))
            raise TransientJobError(
                "V22_COPY_PROVIDER_UNAVAILABLE",
                "The copy service is temporarily unavailable.",
            ) from exc
        if response.status_code >= 500 or response.status_code == 429:
            if claim is not None:
                await cost_ledger.complete(
                    claim.claim_id,
                    outcome="failure",
                    duration_ms=_elapsed_ms(started),
                )
            if permit is not None:
                await self.circuit_breaker.record_failure(permit, now=datetime.now(timezone.utc))
            raise TransientJobError(
                "V22_COPY_PROVIDER_UNAVAILABLE",
                "The copy service is temporarily unavailable.",
            )
        if response.status_code >= 400:
            if claim is not None:
                await cost_ledger.complete(
                    claim.claim_id,
                    outcome="failure",
                    duration_ms=_elapsed_ms(started),
                )
            if permit is not None:
                await self.circuit_breaker.record_failure(
                    permit, now=datetime.now(timezone.utc), eligible=False
                )
            raise DeterministicJobError(
                "V22_COPY_PROVIDER_REJECTED",
                "The copy service rejected the v2.2 request.",
            )
        if len(response.content) > self.max_response_bytes:
            if claim is not None:
                await _complete_dify_claim(
                    cost_ledger,
                    claim.claim_id,
                    None,
                    outcome="failure",
                    started=started,
                )
            if permit is not None:
                await self.circuit_breaker.record_failure(
                    permit, now=datetime.now(timezone.utc), eligible=False
                )
            return {}
        if permit is not None:
            await self.circuit_breaker.record_success(permit)
        try:
            payload = response.json()
            outputs = payload["data"]["outputs"]
        except (KeyError, TypeError, ValueError):
            if claim is not None:
                await _complete_dify_claim(
                    cost_ledger,
                    claim.claim_id,
                    payload if "payload" in locals() else None,
                    outcome="failure",
                    started=started,
                )
            return {}
        valid_outputs = isinstance(outputs, dict)
        if claim is not None:
            await _complete_dify_claim(
                cost_ledger,
                claim.claim_id,
                payload,
                outcome="success" if valid_outputs else "failure",
                started=started,
            )
        return outputs if valid_outputs else {}


def _elapsed_ms(started: float) -> int:
    return max(int((monotonic() - started) * 1000), 0)


def _bounded_token(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000_000:
        return None
    return value


def _dify_usage(payload: object) -> tuple[bool, int | None, int | None, int | None]:
    if not isinstance(payload, dict):
        return False, None, None, None
    data = payload.get("data")
    metadata = payload.get("metadata")
    candidates: list[object] = []
    if isinstance(data, dict):
        candidates.extend((data.get("usage"), data.get("metadata")))
    if isinstance(metadata, dict):
        candidates.append(metadata.get("usage"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        input_tokens = _bounded_token(
            candidate.get("input_tokens", candidate.get("prompt_tokens"))
        )
        output_tokens = _bounded_token(
            candidate.get("output_tokens", candidate.get("completion_tokens"))
        )
        total_tokens = _bounded_token(candidate.get("total_tokens"))
        if (
            input_tokens is not None
            and output_tokens is not None
            and total_tokens == input_tokens + output_tokens
        ):
            return True, input_tokens, output_tokens, total_tokens
    return False, None, None, None


async def _complete_dify_claim(
    ledger: JobCostLedger,
    claim_id: UUID,
    payload: Any,
    *,
    outcome: str,
    started: float,
) -> None:
    usage_known, input_tokens, output_tokens, total_tokens = _dify_usage(payload)
    await ledger.complete(
        claim_id,
        outcome=outcome,
        duration_ms=_elapsed_ms(started),
        usage_known=usage_known,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )
