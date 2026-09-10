"""Strict internal contracts for bounded V2.2 provider cost accounting."""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, RootModel, model_validator

from app.report_v22.models import StrictModel


CostProvider = Literal[
    "serpapi",
    "firecrawl",
    "jina",
    "pagespeed",
    "gsc",
    "ga4",
    "gbp",
    "dify",
]
CostOperation = Literal[
    "serpapi_location",
    "serpapi_market_search",
    "serpapi_public_profile",
    "firecrawl_map",
    "jina_fetch",
    "pagespeed_analysis",
    "gsc_search_analytics",
    "ga4_key_events",
    "ga4_run_report",
    "gbp_location",
    "gbp_performance",
    "gbp_keywords",
    "dify_workflow",
]
CostOutcome = Literal["success", "failure"]


PROVIDERS: tuple[CostProvider, ...] = (
    "serpapi",
    "firecrawl",
    "jina",
    "pagespeed",
    "gsc",
    "ga4",
    "gbp",
    "dify",
)
OPERATION_PROVIDER: dict[CostOperation, CostProvider] = {
    "serpapi_location": "serpapi",
    "serpapi_market_search": "serpapi",
    "serpapi_public_profile": "serpapi",
    "firecrawl_map": "firecrawl",
    "jina_fetch": "jina",
    "pagespeed_analysis": "pagespeed",
    "gsc_search_analytics": "gsc",
    "ga4_key_events": "ga4",
    "ga4_run_report": "ga4",
    "gbp_location": "gbp",
    "gbp_performance": "gbp",
    "gbp_keywords": "gbp",
    "dify_workflow": "dify",
}
OPERATION_LIMITS: dict[CostOperation, int] = {
    "serpapi_location": 1,
    "serpapi_market_search": 30,
    "serpapi_public_profile": 15,
    "firecrawl_map": 4,
    "jina_fetch": 0,
    "pagespeed_analysis": 5,
    "gsc_search_analytics": 12,
    "ga4_key_events": 20,
    "ga4_run_report": 8,
    "gbp_location": 1,
    "gbp_performance": 1,
    "gbp_keywords": 10,
    "dify_workflow": 3,
}
MAX_LEDGER_CLAIMS = 100
MAX_COUNTER_VALUE = 10**15


def empty_request_prices() -> dict[CostProvider, int | None]:
    return {provider: None for provider in PROVIDERS}


class PricingCatalog(StrictModel):
    revision: int = Field(default=1, ge=1, le=1_000_000)
    request_usd_micros: dict[CostProvider, int | None] = Field(
        default_factory=empty_request_prices
    )
    dify_input_mtok_usd_micros: int | None = Field(default=None, ge=0, le=MAX_COUNTER_VALUE)
    dify_output_mtok_usd_micros: int | None = Field(default=None, ge=0, le=MAX_COUNTER_VALUE)

    @model_validator(mode="after")
    def require_complete_provider_catalog(self) -> "PricingCatalog":
        if set(self.request_usd_micros) != set(PROVIDERS):
            raise ValueError("request price catalog must contain every provider exactly once")
        return self


class CostClaim(StrictModel):
    claim_id: UUID
    operation: CostOperation
    provider: CostProvider
    claimed_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    outcome: CostOutcome | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    billable_units: int = Field(default=1, ge=1, le=1_000_000)
    usage_known: bool = False
    input_tokens: int | None = Field(default=None, ge=0, le=100_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=100_000_000)
    total_tokens: int | None = Field(default=None, ge=0, le=200_000_000)

    @model_validator(mode="after")
    def validate_claim(self) -> "CostClaim":
        if OPERATION_PROVIDER[self.operation] != self.provider:
            raise ValueError("cost operation does not belong to provider")
        completion = (self.completed_at, self.outcome, self.duration_ms)
        if any(value is None for value in completion) != all(value is None for value in completion):
            raise ValueError("claim completion fields must be all present or all absent")
        if self.completed_at is not None and self.completed_at < self.claimed_at:
            raise ValueError("claim completion cannot precede claim")
        tokens = (self.input_tokens, self.output_tokens, self.total_tokens)
        if self.provider != "dify" and (self.usage_known or any(value is not None for value in tokens)):
            raise ValueError("token usage is only valid for Dify")
        if self.usage_known:
            if any(value is None for value in tokens):
                raise ValueError("known Dify usage requires input, output and total tokens")
            if self.total_tokens != self.input_tokens + self.output_tokens:  # type: ignore[operator]
                raise ValueError("Dify total tokens must equal input plus output tokens")
        elif any(value is not None for value in tokens):
            raise ValueError("unknown Dify usage cannot contain token values")
        return self

    @property
    def completed(self) -> bool:
        return self.outcome is not None


class CostLedgerState(StrictModel):
    schema_version: Literal["v22_cost_ledger_v1"] = "v22_cost_ledger_v1"
    job_id: UUID
    revision: int = Field(default=1, ge=1)
    pricing: PricingCatalog
    created_at: AwareDatetime
    updated_at: AwareDatetime
    claims: list[CostClaim] = Field(default_factory=list, max_length=MAX_LEDGER_CLAIMS)
    local_denials: dict[CostOperation, int] = Field(default_factory=dict)
    checkpoint_hits: int = Field(default=0, ge=0, le=1_000_000)
    job_attempts: int = Field(default=0, ge=0, le=100)
    job_active_elapsed_ms: int = Field(default=0, ge=0, le=MAX_COUNTER_VALUE)

    @model_validator(mode="after")
    def validate_state(self) -> "CostLedgerState":
        if self.updated_at < self.created_at:
            raise ValueError("ledger update cannot precede creation")
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("cost claim IDs must be unique")
        for operation, count in self.local_denials.items():
            if operation not in OPERATION_PROVIDER or count < 0 or count > 1_000_000:
                raise ValueError("invalid local denial counter")
        return self


BASE_COUNTER_KEYS = frozenset(
    {
        "cost_schema_version",
        "cost_ledger_revision",
        "pricing_revision",
        "job_wall_elapsed_ms",
        "job_active_elapsed_ms",
        "job_attempts",
        "job_retry_count",
        "provider_attempts_total",
        "provider_successes_total",
        "provider_failures_total",
        "provider_outcome_unknown_total",
        "provider_local_denials_total",
        "checkpoint_hits_total",
        "estimated_cost_usd_micros",
        "pricing_unknown_units_total",
        "dify_input_tokens",
        "dify_output_tokens",
        "dify_total_tokens",
        "dify_usage_unknown_calls",
    }
)
PROVIDER_COUNTER_KEYS = frozenset(
    f"{provider}_{metric}"
    for provider in PROVIDERS
    for metric in (
        "attempts",
        "successes",
        "failures",
        "outcome_unknown",
        "local_denials",
        "duration_ms",
        "billable_units",
        "estimated_cost_usd_micros",
        "pricing_unknown_units",
    )
)
ALLOWED_COUNTER_KEYS = BASE_COUNTER_KEYS | PROVIDER_COUNTER_KEYS


class CostCountersV1(RootModel[dict[str, int]]):
    """Flat numeric counters accepted by the existing signed callback contract."""

    @model_validator(mode="after")
    def validate_counters(self) -> "CostCountersV1":
        if not set(self.root) <= ALLOWED_COUNTER_KEYS:
            raise ValueError("cost counters contain an unknown key")
        if set(self.root) != ALLOWED_COUNTER_KEYS:
            raise ValueError("cost counters must contain the complete V1 key set")
        for value in self.root.values():
            if isinstance(value, bool) or not isinstance(value, int) or not isfinite(value):
                raise ValueError("cost counters must be finite integers")
            if value < 0 or value > MAX_COUNTER_VALUE:
                raise ValueError("cost counter is outside the safe range")
        if self.root["cost_schema_version"] != 1:
            raise ValueError("unsupported cost counter schema")
        return self


def microdollars_for_tokens(tokens: int, rate_per_million: int) -> int:
    """Return a conservative integer-microdollar estimate, rounded upward."""

    if tokens < 0 or rate_per_million < 0:
        raise ValueError("token pricing inputs must be non-negative")
    if tokens == 0 or rate_per_million == 0:
        return 0
    return (tokens * rate_per_million + 999_999) // 1_000_000


def cost_counters_from_state(state: CostLedgerState, *, now: datetime) -> CostCountersV1:
    if now.tzinfo is None:
        raise ValueError("cost snapshot time must be timezone-aware")
    wall_ms = max(int((now - state.created_at).total_seconds() * 1000), 0)
    counters = {key: 0 for key in ALLOWED_COUNTER_KEYS}
    counters.update(
        {
            "cost_schema_version": 1,
            "cost_ledger_revision": state.revision,
            "pricing_revision": state.pricing.revision,
            "job_wall_elapsed_ms": wall_ms,
            "job_active_elapsed_ms": state.job_active_elapsed_ms,
            "job_attempts": state.job_attempts,
            "job_retry_count": max(state.job_attempts - 1, 0),
            "checkpoint_hits_total": state.checkpoint_hits,
        }
    )

    for operation, count in state.local_denials.items():
        provider = OPERATION_PROVIDER[operation]
        counters[f"{provider}_local_denials"] += count
        counters["provider_local_denials_total"] += count

    for claim in state.claims:
        provider = claim.provider
        counters[f"{provider}_attempts"] += 1
        counters["provider_attempts_total"] += 1
        counters[f"{provider}_billable_units"] += claim.billable_units
        if claim.outcome == "success":
            counters[f"{provider}_successes"] += 1
            counters["provider_successes_total"] += 1
        elif claim.outcome == "failure":
            counters[f"{provider}_failures"] += 1
            counters["provider_failures_total"] += 1
        else:
            counters[f"{provider}_outcome_unknown"] += 1
            counters["provider_outcome_unknown_total"] += 1
        counters[f"{provider}_duration_ms"] += claim.duration_ms or 0

        request_rate = state.pricing.request_usd_micros[provider]
        claim_cost = 0
        claim_unknown = 0
        if request_rate is None:
            claim_unknown += claim.billable_units
        else:
            claim_cost += request_rate * claim.billable_units

        if provider == "dify":
            if claim.usage_known:
                input_tokens = claim.input_tokens or 0
                output_tokens = claim.output_tokens or 0
                counters["dify_input_tokens"] += input_tokens
                counters["dify_output_tokens"] += output_tokens
                counters["dify_total_tokens"] += claim.total_tokens or 0
                if input_tokens:
                    rate = state.pricing.dify_input_mtok_usd_micros
                    if rate is None:
                        claim_unknown += input_tokens
                    else:
                        claim_cost += microdollars_for_tokens(input_tokens, rate)
                if output_tokens:
                    rate = state.pricing.dify_output_mtok_usd_micros
                    if rate is None:
                        claim_unknown += output_tokens
                    else:
                        claim_cost += microdollars_for_tokens(output_tokens, rate)
            else:
                counters["dify_usage_unknown_calls"] += 1

        counters[f"{provider}_estimated_cost_usd_micros"] += claim_cost
        counters[f"{provider}_pricing_unknown_units"] += claim_unknown
        counters["estimated_cost_usd_micros"] += claim_cost
        counters["pricing_unknown_units_total"] += claim_unknown

    return CostCountersV1(counters)


def pricing_catalog_from_settings(settings) -> PricingCatalog:
    """Freeze the current server-only price configuration into a job catalog."""

    return PricingCatalog(
        revision=settings.V22_COST_PRICING_REVISION,
        request_usd_micros={
            "serpapi": settings.V22_COST_SERPAPI_REQUEST_USD_MICROS,
            "firecrawl": settings.V22_COST_FIRECRAWL_REQUEST_USD_MICROS,
            "jina": settings.V22_COST_JINA_REQUEST_USD_MICROS,
            "pagespeed": settings.V22_COST_PAGESPEED_REQUEST_USD_MICROS,
            "gsc": settings.V22_COST_GSC_REQUEST_USD_MICROS,
            "ga4": settings.V22_COST_GA4_REQUEST_USD_MICROS,
            "gbp": settings.V22_COST_GBP_REQUEST_USD_MICROS,
            "dify": settings.V22_COST_DIFY_REQUEST_USD_MICROS,
        },
        dify_input_mtok_usd_micros=settings.V22_COST_DIFY_INPUT_MTOK_USD_MICROS,
        dify_output_mtok_usd_micros=settings.V22_COST_DIFY_OUTPUT_MTOK_USD_MICROS,
    )
