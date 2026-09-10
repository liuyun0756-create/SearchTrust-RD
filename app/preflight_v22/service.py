"""Orchestration and frozen-response construction for v2.2 preflight."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import SecretStr

from app.api.v2.models import (
    DataGap,
    ModuleAvailability,
    PreflightRequest,
    PreflightResponse,
)
from app.preflight_v22.cache import PreflightCache
from app.preflight_v22.candidates import CandidateSet, build_candidates
from app.preflight_v22.extractors import SiteSignals, extract_site_signals
from app.preflight_v22.fetcher import BoundedHomepageFetcher, HomepageFetchError, HomepageSnapshot
from app.preflight_v22.gbp import GbpLookupResult, GoogleMapsUrlExpander, LimitedGbpLookup
from app.security_v22.logging import digest_suffix
from app.security_v22.urls import UrlUnreachableError, normalize_site_url, validate_gbp_url
from app.core.config import settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SiteFailure:
    code: str
    message: str


def _empty_signals() -> SiteSignals:
    return SiteSignals((), (), (), (), (), None)


def _gap(code: str, message: str, *, blocking: bool, resolution: str) -> DataGap:
    return DataGap(
        gap_code=code,
        message=message,
        blocking=blocking,
        resolution=resolution,
    )


class PreflightService:
    def __init__(
        self,
        *,
        fetcher: BoundedHomepageFetcher,
        gbp_lookup: LimitedGbpLookup,
        cache: PreflightCache,
        serpapi_configured: bool,
        pagespeed_configured: bool,
    ) -> None:
        self.fetcher = fetcher
        self.gbp_lookup = gbp_lookup
        self.cache = cache
        self.serpapi_configured = serpapi_configured
        self.pagespeed_configured = pagespeed_configured

    async def run(self, request: PreflightRequest) -> PreflightResponse:
        started = time.monotonic()
        normalized_input_url = normalize_site_url(str(request.site_url))
        normalized_domain = (urlsplit(normalized_input_url).hostname or "").removeprefix("www.")
        domain_digest = digest_suffix(normalized_domain)
        user_gbp_url = validate_gbp_url(str(request.gbp_url)) if request.gbp_url else None
        cache_key = self.cache.key(request, normalized_input_url)
        cached = await self.cache.get(cache_key)
        if cached is not None:
            logger.info(
                "v2.2 preflight cache_hit domain_digest=%s preflight_id=%s",
                domain_digest,
                cached.preflight_id,
            )
            return cached

        site_snapshot: HomepageSnapshot | None = None
        site_failure: _SiteFailure | None = None
        fetch_started = time.monotonic()
        try:
            site_snapshot = await self.fetcher.fetch(normalized_input_url)
        except (HomepageFetchError, UrlUnreachableError) as exc:
            site_failure = _SiteFailure(exc.code, exc.user_message)
        fetch_ms = round((time.monotonic() - fetch_started) * 1000)

        normalized_site_url = (
            site_snapshot.normalized_site_url if site_snapshot else normalized_input_url
        )
        signals = extract_site_signals(site_snapshot.html) if site_snapshot else _empty_signals()
        selected_gbp_url = user_gbp_url or signals.gbp_url
        if selected_gbp_url:
            selected_gbp_url = validate_gbp_url(selected_gbp_url)
        gbp_started = time.monotonic()
        gbp_result = await self.gbp_lookup.lookup(
            site_url=normalized_site_url,
            signals=signals,
            gbp_url=selected_gbp_url,
        )
        gbp_ms = round((time.monotonic() - gbp_started) * 1000)
        candidates = build_candidates(
            request=request,
            normalized_site_url=normalized_site_url,
            signals=signals,
            gbp_result=gbp_result,
        )
        modules = self._modules(
            site_available=site_snapshot is not None,
            candidates=candidates,
        )
        gaps = self._gaps(
            site_failure=site_failure,
            signals=signals,
            candidates=candidates,
            gbp_result=gbp_result,
        )
        response = PreflightResponse(
            preflight_id=uuid4(),
            normalized_site_url=normalized_site_url,
            identity_candidates=list(candidates.identities),
            service_candidates=list(candidates.services),
            market_candidates=list(candidates.markets),
            competitor_candidates=[],
            module_availability=modules,
            data_gaps=gaps,
            estimated_duration_bucket=self._duration(modules, gaps),
            coverage_summary=self._coverage(candidates, modules, gaps),
        )
        if site_snapshot is not None:
            await self.cache.set(cache_key, response)
        logger.info(
            "v2.2 preflight completed domain_digest=%s preflight_id=%s total_ms=%d "
            "fetch_ms=%d gbp_ms=%d identities=%d services=%d markets=%d gaps=%s",
            domain_digest,
            response.preflight_id,
            round((time.monotonic() - started) * 1000),
            fetch_ms,
            gbp_ms,
            len(response.identity_candidates),
            len(response.service_candidates),
            len(response.market_candidates),
            ",".join(gap.gap_code for gap in response.data_gaps) or "none",
        )
        return response

    def _modules(
        self,
        *,
        site_available: bool,
        candidates: CandidateSet,
    ) -> list[ModuleAvailability]:
        market_ready = bool(candidates.services and candidates.markets and self.serpapi_configured)
        identity_clue = bool(candidates.identities)
        return [
            ModuleAvailability(
                module_key="site_inventory",
                available=site_available,
                reason=(
                    "The public homepage is available for site inventory."
                    if site_available else "The public homepage is not currently available."
                ),
            ),
            ModuleAvailability(
                module_key="site_deep_analysis",
                available=site_available,
                reason=(
                    "The public homepage is available for later bounded site analysis."
                    if site_available else "Site analysis requires an accessible public homepage."
                ),
            ),
            *[
                ModuleAvailability(
                    module_key=key,
                    available=market_ready,
                    reason=(
                        "Service, market, and public search provider prerequisites are available."
                        if market_ready
                        else "Service, market, or public search provider prerequisites are missing."
                    ),
                )
                for key in ("serp_maps", "serp_local_pack", "serp_organic")
            ],
            ModuleAvailability(
                module_key="public_gbp",
                available=bool(self.serpapi_configured and identity_clue),
                reason=(
                    "Public GBP lookup is configured and an identity candidate is available."
                    if self.serpapi_configured and identity_clue
                    else "Public GBP lookup configuration or identity evidence is missing."
                ),
            ),
            ModuleAvailability(
                module_key="competitor_analysis",
                available=market_ready,
                reason=(
                    "Market prerequisites are available for later competitor discovery."
                    if market_ready else "Competitor discovery requires service and market prerequisites."
                ),
            ),
            ModuleAvailability(
                module_key="pagespeed",
                available=bool(site_available and self.pagespeed_configured),
                reason=(
                    "The site and PageSpeed collection capability are available."
                    if site_available and self.pagespeed_configured
                    else "PageSpeed requires an accessible site and configured collection capability."
                ),
            ),
        ]

    def _gaps(
        self,
        *,
        site_failure: _SiteFailure | None,
        signals: SiteSignals,
        candidates: CandidateSet,
        gbp_result: GbpLookupResult,
    ) -> list[DataGap]:
        gaps: list[DataGap] = []
        if site_failure:
            gaps.append(_gap(
                site_failure.code,
                site_failure.message,
                blocking=True,
                resolution="Confirm the public website URL and try preflight again.",
            ))
        if not candidates.identities or any(item.requires_confirmation for item in candidates.identities):
            gaps.append(_gap(
                "BUSINESS_IDENTITY_UNCONFIRMED",
                "A complete business identity has not been independently confirmed.",
                blocking=True,
                resolution="Confirm one business identity candidate before analysis.",
            ))
        has_operating_model = bool(
            signals.operating_models
            or any(candidate.business.operating_model for candidate in candidates.identities)
        )
        if not has_operating_model:
            gaps.append(_gap(
                "OPERATING_MODEL_MISSING",
                "The public evidence did not establish a storefront or service-area model.",
                blocking=True,
                resolution="Confirm whether the business is storefront, service-area, or hybrid.",
            ))
        if not candidates.services:
            gaps.append(_gap(
                "PRIMARY_SERVICE_MISSING",
                "No primary service candidate is available.",
                blocking=True,
                resolution="Provide the primary service for this analysis.",
            ))
        if not candidates.markets:
            gaps.append(_gap(
                "TARGET_MARKET_MISSING",
                "No complete target market candidate is available.",
                blocking=True,
                resolution="Provide a target market including a two-letter country code.",
            ))
        if gbp_result.status != "found":
            gaps.append(_gap(
                gbp_result.code,
                gbp_result.message,
                blocking=False,
                resolution="Provide a supported public Google Maps URL or continue without public GBP data.",
            ))
        if not self.serpapi_configured:
            gaps.append(_gap(
                "SERP_PROVIDER_UNAVAILABLE",
                "Public search collection is not currently configured.",
                blocking=False,
                resolution="Configure the approved public search provider before market collection.",
            ))
        if not self.pagespeed_configured:
            gaps.append(_gap(
                "PAGESPEED_UNAVAILABLE",
                "PageSpeed collection is not currently configured.",
                blocking=False,
                resolution="Configure PageSpeed collection before performance analysis.",
            ))
        gaps.append(_gap(
            "COMPETITOR_DISCOVERY_PENDING",
            "Competitor discovery has not run during this low-cost preflight.",
            blocking=False,
            resolution="Run the market and competitor discovery step before formal analysis.",
        ))
        return gaps

    @staticmethod
    def _duration(
        modules: list[ModuleAvailability],
        gaps: list[DataGap],
    ) -> str:
        if any(gap.blocking for gap in gaps):
            return "under_5_minutes"
        available_count = sum(item.available for item in modules)
        if available_count >= 7:
            return "10_to_15_minutes"
        if available_count >= 3:
            return "5_to_10_minutes"
        return "under_5_minutes"

    @staticmethod
    def _coverage(
        candidates: CandidateSet,
        modules: list[ModuleAvailability],
        gaps: list[DataGap],
    ) -> str:
        available_count = sum(item.available for item in modules)
        blocking_codes = [gap.gap_code for gap in gaps if gap.blocking]
        summary = (
            f"Preflight identified {len(candidates.identities)} business identity candidate(s), "
            f"{len(candidates.services)} service candidate(s), and "
            f"{len(candidates.markets)} market candidate(s). "
            f"{available_count} of 8 later data modules currently meet their prerequisites."
        )
        if blocking_codes:
            summary += " Confirmation gaps remain: " + ", ".join(blocking_codes) + "."
        return summary


def _secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def build_preflight_service(redis=None) -> PreflightService:
    """Build the stateless service; Redis remains an optional optimization."""

    fetcher = BoundedHomepageFetcher(
        connect_timeout=settings.V22_PREFLIGHT_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.V22_PREFLIGHT_READ_TIMEOUT_SECONDS,
        total_timeout=settings.V22_PREFLIGHT_TOTAL_TIMEOUT_SECONDS,
        max_redirects=settings.V22_PREFLIGHT_MAX_REDIRECTS,
        max_response_bytes=settings.V22_PREFLIGHT_MAX_RESPONSE_BYTES,
    )
    gbp_url_expander = GoogleMapsUrlExpander(
        connect_timeout=settings.V22_PREFLIGHT_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.V22_PREFLIGHT_READ_TIMEOUT_SECONDS,
        max_redirects=settings.V22_PREFLIGHT_MAX_REDIRECTS,
    )
    gbp_lookup = LimitedGbpLookup(url_expander=gbp_url_expander.expand)
    cache = PreflightCache(
        redis,
        prefix=settings.V22_REDIS_PREFIX,
        ttl_seconds=settings.V22_PREFLIGHT_CACHE_TTL_SECONDS,
    )
    return PreflightService(
        fetcher=fetcher,
        gbp_lookup=gbp_lookup,
        cache=cache,
        serpapi_configured=gbp_lookup.configured,
        pagespeed_configured=bool(_secret_value(settings.PAGESPEED_API_KEY)),
    )
