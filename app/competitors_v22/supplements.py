"""Bounded public-homepage verification for manually supplemented competitors."""

from __future__ import annotations

from app.competitors_v22.normalization import (
    normalize_domain,
    normalize_name,
    tokenize_relevance_text,
)
from app.preflight_v22.extractors import extract_site_signals
from app.preflight_v22.fetcher import BoundedHomepageFetcher, HomepageFetchError
from app.preflight_v22.urls import UrlUnreachableError
from app.report_v22.models import TargetMarket


class SupplementalHomepageValidator:
    def __init__(self, fetcher: BoundedHomepageFetcher) -> None:
        self.fetcher = fetcher

    async def validate(
        self,
        *,
        website_url: str,
        expected_name: str,
        primary_service: str,
        target_market: TargetMarket,
    ) -> bool:
        try:
            snapshot = await self.fetcher.fetch(website_url)
        except (HomepageFetchError, UrlUnreachableError):
            return False
        if normalize_domain(snapshot.source_url) != normalize_domain(website_url):
            return False

        signals = extract_site_signals(snapshot.html)
        expected_normalized_name = normalize_name(expected_name)
        name_match = any(
            normalize_name(signal.value) == expected_normalized_name for signal in signals.names
        )
        expected_service = tokenize_relevance_text(primary_service)
        observed_services = set().union(
            *(tokenize_relevance_text(signal.value) for signal in signals.services),
            tokenize_relevance_text(snapshot.html[:50_000]),
        )
        service_match = bool(expected_service & observed_services)
        market_tokens = tokenize_relevance_text(
            " ".join(
                value
                for value in (
                    target_market.display_name,
                    target_market.city,
                    target_market.region,
                    target_market.postal_code,
                )
                if value
            )
        )
        observed_market = set().union(
            *(
                tokenize_relevance_text(
                    f"{signal.display_name} {signal.city or ''} {signal.region or ''} "
                    f"{signal.postal_code or ''}"
                )
                for signal in signals.markets
            ),
            tokenize_relevance_text(snapshot.html[:50_000]),
        )
        market_match = not market_tokens or bool(market_tokens & observed_market)
        return name_match or (service_match and market_match)
