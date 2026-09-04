"""Checkpointed worker-stage adapter for v2.2 SERP market collection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

import httpx
from pydantic import AwareDatetime, Field, ValidationError, model_validator

from app.api.v2.models import AnalyzeRequest
from app.collectors.serp_market import (
    SerpMarketCollectionError,
    SerpMarketCollector,
    SerpMarketProviderError,
    SerpProviderResponse,
    sanitize_serp_payload,
)
from app.collectors.serp_market_location import (
    LocationProvider,
    SerpApiLocationProvider,
    SerpLocationResolutionError,
    resolve_target_point,
)
from app.collectors.serp_market_models import (
    SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL,
    SerpMarketContext,
    SerpMarketSnapshot,
    SerpTargetPoint,
)
from app.collectors.serp_market_requests import (
    SerpPlannedCall,
    build_serp_search_plan,
    serp_market_context_from_analyze,
)
from app.core.config import Settings
from app.integrations.serpapi import (
    BeforeAttempt,
    SerpApiHttpError,
    SerpApiInvalidResponse,
    SerpApiKeyState,
    SerpApiKeysUnavailable,
    SerpApiTransportError,
    configured_serpapi_keys,
    execute_serpapi_get,
)
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.models import StrictModel


_CHECKPOINT_VERSION = "serp_market_v1"
_MAX_NORMALIZED_CHECKPOINT_BYTES = 500_000
logger = logging.getLogger(__name__)


class AttemptAwareProvider(Protocol):
    async def search(
        self,
        call: SerpPlannedCall,
        *,
        before_attempt: BeforeAttempt,
    ) -> SerpProviderResponse: ...


class SerpMarketCheckpointError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_SERP_MARKET_CHECKPOINT_INVALID",
            "A saved SERP market checkpoint could not be validated.",
        )


class SerpProviderAttemptsExhausted(RuntimeError):
    pass


class _AttemptClaim(StrictModel):
    schema_version: str = Field(pattern=r"^serp_provider_attempt_v1$")
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    attempt_number: int = Field(ge=1, le=SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL)
    key_slot: int = Field(ge=1, le=3)
    key_fingerprint: str = Field(pattern=r"^[a-f0-9]{12}$")
    claimed_at: AwareDatetime


class _ProviderCheckpoint(StrictModel):
    schema_version: str = Field(pattern=r"^serp_provider_response_v1$")
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    engine: str = Field(pattern=r"^(google_maps|google)$")
    payload: dict[str, Any]
    payload_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    provider_attempts: int = Field(ge=1, le=SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_time(self) -> "_ProviderCheckpoint":
        if self.completed_at < self.started_at:
            raise ValueError("provider completion cannot precede start")
        return self


class _LocationCheckpoint(StrictModel):
    schema_version: str = Field(pattern=r"^serp_location_response_v1$")
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    point: SerpTargetPoint


class _SnapshotCheckpoint(StrictModel):
    schema_version: str = Field(pattern=r"^serp_market_snapshot_checkpoint_v1$")
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    snapshot: SerpMarketSnapshot


def _validate_model(model_type: Any, value: Any) -> Any:
    try:
        return model_type.model_validate_json(canonical_json_bytes(value))
    except (TypeError, ValidationError, ValueError) as exc:
        raise SerpMarketCheckpointError() from exc


class ProviderAttemptLedger:
    def __init__(
        self,
        *,
        checkpoints: JobCheckpoints,
        job_id: UUID,
        clock: Callable[[], datetime],
    ) -> None:
        self.checkpoints = checkpoints
        self.job_id = job_id
        self.clock = clock

    def key(self, call: SerpPlannedCall, attempt_number: int) -> str:
        return (
            f"{_CHECKPOINT_VERSION}:attempt:{call.request_digest[7:]}:"
            f"{attempt_number}"
        )

    async def _existing(self, call: SerpPlannedCall) -> list[_AttemptClaim]:
        claims: list[_AttemptClaim] = []
        missing_seen = False
        for attempt_number in range(1, SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL + 1):
            raw = await self.checkpoints.get(self.job_id, self.key(call, attempt_number))
            if raw is None:
                missing_seen = True
                continue
            if missing_seen:
                raise SerpMarketCheckpointError()
            claim = _validate_model(_AttemptClaim, raw)
            if claim.request_digest != call.request_digest or claim.attempt_number != attempt_number:
                raise SerpMarketCheckpointError()
            claims.append(claim)
        return claims

    async def count(self, call: SerpPlannedCall) -> int:
        return len(await self._existing(call))

    async def claim(self, call: SerpPlannedCall, key_slot: int, fingerprint: str) -> int:
        while True:
            claims = await self._existing(call)
            next_number = len(claims) + 1
            if next_number > SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL:
                raise SerpProviderAttemptsExhausted()
            claim = _AttemptClaim(
                schema_version="serp_provider_attempt_v1",
                request_digest=call.request_digest,
                attempt_number=next_number,
                key_slot=key_slot,
                key_fingerprint=fingerprint,
                claimed_at=self.clock(),
            )
            if await self.checkpoints.save(
                self.job_id,
                self.key(call, next_number),
                claim.model_dump(mode="json"),
            ):
                logger.info(
                    "SERP provider attempt claimed call_suffix=%s attempt=%d key_slot=%d",
                    call.request_digest[-12:],
                    next_number,
                    key_slot,
                )
                return next_number


class CheckpointedSerpProvider:
    def __init__(
        self,
        *,
        delegate: AttemptAwareProvider,
        checkpoints: JobCheckpoints,
        job_id: UUID,
        clock: Callable[[], datetime],
    ) -> None:
        self.delegate = delegate
        self.checkpoints = checkpoints
        self.job_id = job_id
        self.clock = clock
        self.attempts = ProviderAttemptLedger(
            checkpoints=checkpoints,
            job_id=job_id,
            clock=clock,
        )

    def checkpoint_key(self, call: SerpPlannedCall) -> str:
        return f"{_CHECKPOINT_VERSION}:search:{call.request_digest[7:]}"

    def _validated_checkpoint(
        self,
        raw: Any,
        call: SerpPlannedCall,
    ) -> _ProviderCheckpoint:
        checkpoint = _validate_model(_ProviderCheckpoint, raw)
        if checkpoint.request_digest != call.request_digest or checkpoint.engine != call.engine:
            raise SerpMarketCheckpointError()
        if len(canonical_json_bytes(checkpoint.payload)) > _MAX_NORMALIZED_CHECKPOINT_BYTES:
            raise SerpMarketCheckpointError()
        if sanitize_serp_payload(checkpoint.payload, call.engine) != checkpoint.payload:
            raise SerpMarketCheckpointError()
        if request_digest(checkpoint.payload) != checkpoint.payload_checksum:
            raise SerpMarketCheckpointError()
        return checkpoint

    def _response(
        self,
        checkpoint: _ProviderCheckpoint,
        *,
        checkpoint_hit: bool,
    ) -> SerpProviderResponse:
        return SerpProviderResponse(
            payload=checkpoint.payload,
            provider_attempts=checkpoint.provider_attempts,
            logical_call_used=True,
            checkpoint_hit=checkpoint_hit,
            started_at=checkpoint.started_at,
            completed_at=checkpoint.completed_at,
        )

    async def search(self, call: SerpPlannedCall) -> SerpProviderResponse:
        key = self.checkpoint_key(call)
        existing = await self.checkpoints.get(self.job_id, key)
        if existing is not None:
            logger.info("SERP checkpoint hit call_suffix=%s", call.request_digest[-12:])
            return self._response(
                self._validated_checkpoint(existing, call),
                checkpoint_hit=True,
            )

        if await self.attempts.count(call) >= SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_ATTEMPTS_EXHAUSTED",
                retryable=False,
                provider_attempts=SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL,
                logical_call_used=True,
            )

        async def before_attempt(key_slot: int, fingerprint: str) -> None:
            await self.attempts.claim(call, key_slot, fingerprint)

        started_at = self.clock()
        try:
            response = await self.delegate.search(call, before_attempt=before_attempt)
        except SerpProviderAttemptsExhausted as exc:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_ATTEMPTS_EXHAUSTED",
                retryable=False,
                provider_attempts=SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL,
                logical_call_used=True,
            ) from exc
        except SerpMarketProviderError as exc:
            attempts = await self.attempts.count(call)
            raise SerpMarketProviderError(
                exc.code,
                retryable=exc.retryable,
                provider_attempts=attempts,
                logical_call_used=attempts > 0,
            ) from exc

        attempts = await self.attempts.count(call)
        if attempts == 0:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_ATTEMPT_AUDIT_MISSING",
                retryable=False,
                provider_attempts=0,
                logical_call_used=False,
            )
        payload = sanitize_serp_payload(response.payload, call.engine)
        checkpoint = _ProviderCheckpoint(
            schema_version="serp_provider_response_v1",
            request_digest=call.request_digest,
            engine=call.engine,
            payload=payload,
            payload_checksum=request_digest(payload),
            provider_attempts=attempts,
            started_at=response.started_at or started_at,
            completed_at=response.completed_at or self.clock(),
        )
        if len(canonical_json_bytes(checkpoint.payload)) > _MAX_NORMALIZED_CHECKPOINT_BYTES:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_RESPONSE_TOO_LARGE",
                retryable=False,
                provider_attempts=attempts,
                logical_call_used=True,
            )
        await self.checkpoints.save(
            self.job_id,
            key,
            checkpoint.model_dump(mode="json"),
        )
        persisted = await self.checkpoints.get(self.job_id, key)
        canonical = checkpoint if persisted is None else self._validated_checkpoint(persisted, call)
        return self._response(canonical, checkpoint_hit=False)


class _ResponseTooLarge(RuntimeError):
    pass


class _BoundedHttpClient:
    def __init__(self, client: httpx.AsyncClient, *, max_response_bytes: int) -> None:
        self.client = client
        self.max_response_bytes = max_response_bytes

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        # SerpAPI can occasionally advertise a compressed response while
        # returning an uncompressed body.  Requesting identity encoding keeps
        # httpx from rejecting an otherwise valid JSON payload while it is
        # being streamed through the response-size guard below.
        headers = httpx.Headers(kwargs.pop("headers", None))
        headers["Accept-Encoding"] = "identity"
        async with self.client.stream("GET", url, headers=headers, **kwargs) as response:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise _ResponseTooLarge()
                chunks.append(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=response.request,
            )


class SerpApiSearchAdapter:
    def __init__(
        self,
        *,
        keys: list[str],
        base_url: str,
        connect_timeout: int,
        read_timeout: int,
        total_timeout: int,
        max_response_bytes: int,
        clock: Callable[[], datetime] | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.keys = configured_serpapi_keys(*keys)
        self.base_url = base_url
        self.total_timeout = total_timeout
        self.max_response_bytes = max_response_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.state = SerpApiKeyState()
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            )
        )

    async def search(
        self,
        call: SerpPlannedCall,
        *,
        before_attempt: BeforeAttempt,
    ) -> SerpProviderResponse:
        if not self.keys:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_NOT_CONFIGURED",
                retryable=False,
                provider_attempts=0,
                logical_call_used=False,
            )
        started_at = self.clock()
        try:
            async with asyncio.timeout(self.total_timeout):
                async with self.client_factory() as client:
                    bounded = _BoundedHttpClient(
                        client,
                        max_response_bytes=self.max_response_bytes,
                    )
                    result = await execute_serpapi_get(
                        bounded,
                        call.params,
                        keys=self.keys,
                        base_url=self.base_url,
                        state=self.state,
                        before_attempt=before_attempt,
                    )
        except SerpProviderAttemptsExhausted:
            raise
        except SerpApiKeysUnavailable as exc:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_KEYS_UNAVAILABLE",
                retryable=True,
            ) from exc
        except SerpApiHttpError as exc:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_TEMPORARY" if exc.status_code >= 500 else "SERP_PROVIDER_REJECTED",
                retryable=exc.status_code >= 500,
            ) from exc
        except SerpApiInvalidResponse as exc:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_RESPONSE_INVALID",
                retryable=False,
            ) from exc
        except SerpApiTransportError as exc:
            if isinstance(exc.__cause__, _ResponseTooLarge):
                raise SerpMarketProviderError(
                    "SERP_PROVIDER_RESPONSE_TOO_LARGE",
                    retryable=False,
                ) from exc
            raise SerpMarketProviderError(
                "SERP_PROVIDER_TEMPORARY",
                retryable=True,
            ) from exc
        except TimeoutError as exc:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_TEMPORARY",
                retryable=True,
            ) from exc
        return SerpProviderResponse(
            payload=result.payload,
            provider_attempts=result.metadata.attempt_count,
            logical_call_used=True,
            started_at=started_at,
            completed_at=self.clock(),
        )


class CheckpointedSerpMarketStage:
    def __init__(
        self,
        *,
        location_provider: LocationProvider,
        search_provider: AttemptAwareProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.location_provider = location_provider
        self.search_provider = search_provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _target_point(
        self,
        *,
        job_id: UUID,
        context: SerpMarketContext,
        checkpoints: JobCheckpoints,
    ) -> tuple[SerpTargetPoint, int]:
        market = context.target_market
        if market.latitude is not None or market.longitude is not None:
            try:
                return (
                    await resolve_target_point(market, provider=None, clock=self.clock),
                    0,
                )
            except SerpLocationResolutionError as exc:
                raise DeterministicJobError(
                    f"V22_{exc.code}",
                    exc.user_message,
                ) from exc

        identity = request_digest(market.model_dump(mode="json"))
        key = f"{_CHECKPOINT_VERSION}:location:{identity[7:]}"

        async def operation() -> dict[str, Any]:
            try:
                point = await resolve_target_point(
                    market,
                    provider=self.location_provider,
                    clock=self.clock,
                )
            except SerpLocationResolutionError as exc:
                error_type = TransientJobError if exc.retryable else DeterministicJobError
                raise error_type(f"V22_{exc.code}", exc.user_message) from exc
            return _LocationCheckpoint(
                schema_version="serp_location_response_v1",
                request_digest=identity,
                point=point,
            ).model_dump(mode="json")

        raw = await checkpoints.run_once(job_id, key, operation)
        checkpoint = _validate_model(_LocationCheckpoint, raw)
        if checkpoint.request_digest != identity:
            raise SerpMarketCheckpointError()
        return checkpoint.point, 1

    async def collect(
        self,
        *,
        job_id: UUID,
        request: AnalyzeRequest,
        checkpoints: JobCheckpoints,
    ) -> SerpMarketSnapshot:
        return await self.collect_context(
            job_id=job_id,
            context=serp_market_context_from_analyze(request),
            checkpoints=checkpoints,
        )

    async def collect_context(
        self,
        *,
        job_id: UUID,
        context: SerpMarketContext,
        checkpoints: JobCheckpoints,
    ) -> SerpMarketSnapshot:
        identity = request_digest(
            {
                "queries": context.queries,
                "target_market": context.target_market.model_dump(mode="json"),
                "device": context.device,
                "language": context.language,
            }
        )
        final_key = f"{_CHECKPOINT_VERSION}:snapshot:{identity[7:]}"
        existing = await checkpoints.get(job_id, final_key)
        if existing is not None:
            final_checkpoint = _validate_model(_SnapshotCheckpoint, existing)
            if final_checkpoint.request_digest != identity:
                raise SerpMarketCheckpointError()
            snapshot = final_checkpoint.snapshot
            if snapshot.job_id != job_id or snapshot.queries != context.queries:
                raise SerpMarketCheckpointError()
            return snapshot

        target_point, location_calls = await self._target_point(
            job_id=job_id,
            context=context,
            checkpoints=checkpoints,
        )
        try:
            plan = build_serp_search_plan(
                queries=context.queries,
                target_point=target_point,
                device=context.device,
                language=context.language,
            )
        except (ValidationError, ValueError) as exc:
            raise DeterministicJobError(
                "V22_SERP_SEARCH_PLAN_INVALID",
                "The SERP search plan is invalid.",
            ) from exc
        checkpointed_provider = CheckpointedSerpProvider(
            delegate=self.search_provider,
            checkpoints=checkpoints,
            job_id=job_id,
            clock=self.clock,
        )
        try:
            snapshot = await SerpMarketCollector(
                provider=checkpointed_provider,
                clock=self.clock,
            ).collect(
                job_id=job_id,
                plan=plan,
                location_resolution_calls=location_calls,
            )
        except SerpMarketCollectionError as exc:
            error_type = TransientJobError if exc.retryable else DeterministicJobError
            raise error_type(f"V22_{exc.code}", exc.user_message) from exc

        raw = await checkpoints.run_once(
            job_id,
            final_key,
            lambda: _return_value(
                _SnapshotCheckpoint(
                    schema_version="serp_market_snapshot_checkpoint_v1",
                    request_digest=identity,
                    snapshot=snapshot,
                ).model_dump(mode="json")
            ),
        )
        final_checkpoint = _validate_model(_SnapshotCheckpoint, raw)
        if final_checkpoint.request_digest != identity:
            raise SerpMarketCheckpointError()
        persisted = final_checkpoint.snapshot
        if persisted.job_id != job_id or persisted.queries != plan.queries:
            raise SerpMarketCheckpointError()
        return persisted


async def _return_value(value: dict[str, Any]) -> dict[str, Any]:
    return value


def serp_market_cost_counters(snapshot: SerpMarketSnapshot) -> dict[str, int]:
    return {
        "serp_logical_calls": snapshot.budget.logical_calls_used,
        "serp_provider_attempts": snapshot.budget.provider_attempts,
        "serp_checkpoint_hits": snapshot.budget.checkpoint_hits,
        "serp_location_calls": snapshot.budget.location_resolution_calls,
    }


def build_serp_market_stage(settings: Settings) -> CheckpointedSerpMarketStage:
    clock = lambda: datetime.now(timezone.utc)
    location_provider = SerpApiLocationProvider(
        url=settings.SERPAPI_LOCATIONS_URL,
        connect_timeout=settings.V22_SERP_MARKET_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.V22_SERP_MARKET_READ_TIMEOUT_SECONDS,
        total_timeout=settings.V22_SERP_MARKET_TOTAL_TIMEOUT_SECONDS,
        max_response_bytes=settings.V22_SERP_MARKET_MAX_RESPONSE_BYTES,
    )
    search_provider = SerpApiSearchAdapter(
        keys=[
            settings.SERPAPI_KEY,
            settings.SERPAPI_KEY_SECONDARY,
            settings.SERPAPI_KEY_TERTIARY,
        ],
        base_url=settings.SERPAPI_BASE_URL,
        connect_timeout=settings.V22_SERP_MARKET_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.V22_SERP_MARKET_READ_TIMEOUT_SECONDS,
        total_timeout=settings.V22_SERP_MARKET_TOTAL_TIMEOUT_SECONDS,
        max_response_bytes=settings.V22_SERP_MARKET_MAX_RESPONSE_BYTES,
        clock=clock,
    )
    return CheckpointedSerpMarketStage(
        location_provider=location_provider,
        search_provider=search_provider,
        clock=clock,
    )
