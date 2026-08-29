"""Checkpointed public GBP/review collection with one shared 15-attempt budget."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

import httpx

from app.api.v2.models import ConfirmedCompetitor
from app.collectors.serp_market_models import SerpMarketResultRecord, SerpMarketSnapshot
from app.competitors_v22.models import (
    COMPETITOR_PROVIDER_ATTEMPT_LIMIT,
    COMPETITOR_REVIEW_PAGE_LIMIT,
    COMPETITOR_REVIEW_SAMPLE_LIMIT,
    PublicGbpProfile,
    PublicReviewRecord,
)
from app.competitors_v22.normalization import normalize_domain, normalize_name
from app.competitors_v22.public_profile import (
    normalize_place_profile,
    normalize_review_page,
    sanitize_public_profile_payload,
)
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import request_digest
from app.integrations.serpapi import (
    SerpApiKeyState,
    configured_serpapi_keys,
    execute_serpapi_get,
)
from app.core.config import Settings


class PublicProfileProvider(Protocol):
    async def request(self, params: dict[str, str], *, before_attempt) -> dict[str, Any]: ...


class _BoundedClient:
    def __init__(self, client: httpx.AsyncClient, max_response_bytes: int) -> None:
        self.client = client
        self.max_response_bytes = max_response_bytes

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        async with self.client.stream("GET", url, **kwargs) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self.max_response_bytes:
                    raise ValueError("public profile provider response exceeded its safe limit")
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(body),
                request=response.request,
            )


class SerpApiPublicProfileProvider:
    def __init__(
        self,
        *,
        keys: list[str],
        base_url: str,
        connect_timeout: int,
        read_timeout: int,
        total_timeout: int,
        max_response_bytes: int,
    ) -> None:
        self.keys = configured_serpapi_keys(*keys)
        self.base_url = base_url
        self.total_timeout = total_timeout
        self.max_response_bytes = max_response_bytes
        self.state = SerpApiKeyState()
        self.timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )

    async def request(self, params: dict[str, str], *, before_attempt) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            bounded = _BoundedClient(client, self.max_response_bytes)
            response = await asyncio.wait_for(
                execute_serpapi_get(
                    bounded,
                    params,
                    keys=self.keys,
                    base_url=self.base_url,
                    state=self.state,
                    before_attempt=before_attempt,
                ),
                timeout=self.total_timeout,
            )
        return response.payload


class ProviderBudgetExhausted(RuntimeError):
    pass


class SharedProviderAttemptBudget:
    def __init__(self, checkpoints: JobCheckpoints, job_id: UUID, *, limit: int = 15) -> None:
        self.redis = checkpoints.redis
        self.prefix = checkpoints.keys.prefix
        self.ttl_seconds = checkpoints.ttl_seconds
        self.job_id = job_id
        self.limit = min(limit, COMPETITOR_PROVIDER_ATTEMPT_LIMIT)

    def _key(self, slot: int) -> str:
        return f"{self.prefix}:competitor-provider-attempt:{self.job_id}:{slot}"

    async def claim(self, key_slot: int = 0, fingerprint: str = "") -> None:
        for slot in range(1, self.limit + 1):
            if await self.redis.set(
                self._key(slot),
                f"key-slot:{key_slot}:{fingerprint[:12]}",
                ex=self.ttl_seconds,
                nx=True,
            ):
                return
        raise ProviderBudgetExhausted()

    async def count(self) -> int:
        return sum(bool(value) for value in await self.redis.mget([self._key(i) for i in range(1, self.limit + 1)]))


@dataclass(frozen=True)
class CompetitorPublicProfileResult:
    competitor_id: str
    profile: PublicGbpProfile | None
    reviews: tuple[PublicReviewRecord, ...]
    identity_traceable: bool
    profile_status: str
    reviews_status: str
    place_detail_calls: int
    review_page_calls: int
    checkpoint_hits: int
    limitations: tuple[str, ...]


def _matching_records(candidate: ConfirmedCompetitor, snapshot: SerpMarketSnapshot) -> list[SerpMarketResultRecord]:
    domain = normalize_domain(str(candidate.website_url))
    return [record for run in snapshot.query_runs for record in run.results if record.normalized_domain == domain]


def _profile_from_market(records: list[SerpMarketResultRecord], now: datetime) -> PublicGbpProfile | None:
    if not records:
        return None
    best = sorted(records, key=lambda item: item.position)[0]
    if not all((best.address, best.categories, best.rating is not None, best.review_count is not None)):
        return None
    return PublicGbpProfile(
        business_name=best.display_name,
        public_gbp_url=f"https://www.google.com/maps?cid={best.provider_cid}" if best.provider_cid else None,
        provider_place_id=best.provider_place_id,
        provider_data_id=best.provider_data_id,
        provider_cid=best.provider_cid,
        website_url=best.url,
        address=best.address,
        categories=best.categories,
        rating=best.rating,
        review_count=best.review_count,
        collected_at=now,
    )


class CheckpointedPublicProfileStage:
    def __init__(self, provider: PublicProfileProvider, *, clock=None) -> None:
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _request(self, *, job_id, params, checkpoints, budget):
        key = f"competitor-public:{request_digest(params)[7:]}"
        existing = await checkpoints.get(job_id, key)
        if existing is not None:
            if not isinstance(existing, dict) or sanitize_public_profile_payload(existing, params["engine"]) != existing:
                raise ValueError("public profile checkpoint is invalid")
            return existing, True
        payload = await self.provider.request(params, before_attempt=budget.claim)
        if not isinstance(payload, dict):
            raise ValueError("public profile provider returned an invalid payload")
        payload = sanitize_public_profile_payload(payload, params["engine"])
        await checkpoints.save(job_id, key, payload)
        persisted = await checkpoints.get(job_id, key)
        return persisted if isinstance(persisted, dict) else payload, False

    async def collect_one(
        self,
        *,
        job_id: UUID,
        competitor: ConfirmedCompetitor,
        market_snapshot: SerpMarketSnapshot,
        language: str,
        checkpoints: JobCheckpoints,
        budget: SharedProviderAttemptBudget,
    ) -> CompetitorPublicProfileResult:
        records = _matching_records(competitor, market_snapshot)
        strong = next((item for item in records if item.provider_data_id or item.provider_place_id or item.provider_cid), None)
        if strong is None:
            return CompetitorPublicProfileResult(
                competitor.competitor_id, None, (), False, "unavailable", "unavailable", 0, 0, 0,
                ("The competitor public place identity could not be traced.",),
            )
        now = self.clock()
        profile = _profile_from_market(records, now)
        detail_calls = review_calls = checkpoint_hits = 0
        limitations: list[str] = []
        try:
            if profile is None:
                params = {"engine": "google_maps", "type": "place", "hl": language}
                if strong.provider_place_id:
                    params["place_id"] = strong.provider_place_id
                else:
                    params["data"] = strong.provider_data_id or ""
                    params["ll"] = f"@{strong.latitude},{strong.longitude},14z"
                payload, hit = await self._request(job_id=job_id, params=params, checkpoints=checkpoints, budget=budget)
                checkpoint_hits += int(hit)
                detail_calls += int(not hit)
                profile = normalize_place_profile(payload, collected_at=now, params=params)
                if profile and profile.website_url and normalize_domain(str(profile.website_url)) != normalize_domain(str(competitor.website_url)):
                    return CompetitorPublicProfileResult(
                        competitor.competitor_id, None, (), False, "unavailable", "unavailable",
                        detail_calls, 0, checkpoint_hits, ("The public place identity conflicts with the confirmed website.",),
                    )
            data_id = (profile.provider_data_id if profile else None) or strong.provider_data_id
            reviews: list[PublicReviewRecord] = []
            seen: set[str] = set()
            token: str | None = None
            if data_id:
                for page in range(COMPETITOR_REVIEW_PAGE_LIMIT):
                    params = {
                        "engine": "google_maps_reviews",
                        "data_id": data_id,
                        "sort_by": "newestFirst",
                        "hl": language,
                    }
                    if token:
                        params["next_page_token"] = token
                    payload, hit = await self._request(job_id=job_id, params=params, checkpoints=checkpoints, budget=budget)
                    checkpoint_hits += int(hit)
                    review_calls += int(not hit)
                    page_records, next_token = normalize_review_page(payload, collected_at=now, params=params)
                    for review in page_records:
                        key = review.provider_review_id or review.review_record_id
                        if key not in seen:
                            seen.add(key)
                            reviews.append(review)
                        if len(reviews) >= COMPETITOR_REVIEW_SAMPLE_LIMIT:
                            break
                    if len(reviews) >= COMPETITOR_REVIEW_SAMPLE_LIMIT or not next_token or next_token == token:
                        break
                    token = next_token
            return CompetitorPublicProfileResult(
                competitor.competitor_id,
                profile,
                tuple(reviews[:COMPETITOR_REVIEW_SAMPLE_LIMIT]),
                True,
                "available" if profile else "unavailable",
                "available" if reviews else "unavailable",
                detail_calls,
                review_calls,
                checkpoint_hits,
                tuple(limitations),
            )
        except asyncio.CancelledError:
            raise
        except ProviderBudgetExhausted:
            limitations.append("The shared public-provider attempt budget was exhausted.")
        except (OSError, ValueError, RuntimeError):
            limitations.append("Public competitor data was only partially available.")
        return CompetitorPublicProfileResult(
            competitor.competitor_id,
            profile,
            (),
            True,
            "available" if profile else "unavailable",
            "unavailable",
            detail_calls,
            review_calls,
            checkpoint_hits,
            tuple(limitations),
        )


def build_public_profile_stage(settings: Settings) -> CheckpointedPublicProfileStage:
    return CheckpointedPublicProfileStage(
        SerpApiPublicProfileProvider(
            keys=[settings.SERPAPI_KEY, settings.SERPAPI_KEY_SECONDARY, settings.SERPAPI_KEY_TERTIARY],
            base_url=settings.SERPAPI_BASE_URL,
            connect_timeout=settings.V22_COMPETITOR_CONNECT_TIMEOUT_SECONDS,
            read_timeout=settings.V22_COMPETITOR_READ_TIMEOUT_SECONDS,
            total_timeout=settings.V22_COMPETITOR_TOTAL_TIMEOUT_SECONDS,
            max_response_bytes=settings.V22_COMPETITOR_MAX_RESPONSE_BYTES,
        )
    )
