"""Deterministic merging of user, website, and GBP preflight candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.api.v2.models import (
    BusinessIdentityCandidate,
    MarketCandidate,
    PreflightRequest,
    TextCandidate,
)
from app.preflight_v22.extractors import SiteSignals
from app.preflight_v22.gbp import GbpCandidate, GbpLookupResult
from app.report_v22.models import BusinessIdentity, TargetMarket


@dataclass(frozen=True)
class CandidateSet:
    identities: tuple[BusinessIdentityCandidate, ...]
    services: tuple[TextCandidate, ...]
    markets: tuple[MarketCandidate, ...]


def _normalized_text(value: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).split())


def _normalized_domain(value: str | None) -> str:
    if not value:
        return ""
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".").removeprefix("www.")
    except ValueError:
        return ""


def _domains_match(left: str | None, right: str | None) -> bool:
    left_domain = _normalized_domain(left)
    right_domain = _normalized_domain(right)
    return bool(
        left_domain
        and right_domain
        and (
            left_domain == right_domain
            or left_domain.endswith("." + right_domain)
            or right_domain.endswith("." + left_domain)
        )
    )


def _names_match(left: str | None, right: str | None) -> bool:
    left_name = _normalized_text(left)
    right_name = _normalized_text(right)
    return bool(
        left_name
        and right_name
        and (
            left_name == right_name
            or (len(left_name) >= 6 and left_name in right_name)
            or (len(right_name) >= 6 and right_name in left_name)
        )
    )


def _phones_match(left: str | None, right: str | None) -> bool:
    left_phone = re.sub(r"\D", "", left or "")[-10:]
    right_phone = re.sub(r"\D", "", right or "")[-10:]
    return bool(len(left_phone) == 10 and left_phone == right_phone)


def _markets_match(left: TargetMarket | None, right: TargetMarket | None) -> bool:
    if left is None or right is None or left.country_code != right.country_code:
        return False
    if left.city and right.city:
        return left.city.casefold() == right.city.casefold()
    return bool(left.region and right.region and left.region.casefold() == right.region.casefold())


def _text_candidates(request: PreflightRequest, signals: SiteSignals, gbp: GbpLookupResult) -> tuple[TextCandidate, ...]:
    values: list[TextCandidate] = []
    if request.primary_service:
        values.append(TextCandidate(
            value=request.primary_service,
            confidence="high",
            evidence_summary="Provided by the user for preflight confirmation.",
        ))
    values.extend(
        TextCandidate(
            value=signal.value,
            confidence=signal.confidence,
            evidence_summary=f"Observed from {signal.source.replace('_', ' ')} on the public site.",
        )
        for signal in signals.services
    )
    for candidate in gbp.candidates:
        values.extend(
            TextCandidate(
                value=category,
                confidence="medium",
                evidence_summary="Observed in the public GBP category list.",
            )
            for category in candidate.categories
            if len(category) <= 240
        )
    return _dedupe_models(values, lambda item: item.value.casefold())


def _market_candidates(request: PreflightRequest, signals: SiteSignals, gbp: GbpLookupResult) -> tuple[MarketCandidate, ...]:
    values: list[MarketCandidate] = []
    if request.target_market:
        values.append(MarketCandidate(
            market=request.target_market,
            confidence="high",
            evidence_summary="Provided by the user for preflight confirmation.",
        ))
    values.extend(
        MarketCandidate(
            market=TargetMarket(
                display_name=signal.display_name,
                country_code=signal.country_code,
                region=signal.region,
                city=signal.city,
                postal_code=signal.postal_code,
            ),
            confidence=signal.confidence,
            evidence_summary=f"Observed from {signal.source.replace('_', ' ')} on the public site.",
        )
        for signal in signals.markets
    )
    values.extend(
        MarketCandidate(
            market=candidate.market,
            confidence="medium",
            evidence_summary="Observed from the public GBP address.",
        )
        for candidate in gbp.candidates
        if candidate.market is not None
    )
    return _dedupe_models(
        values,
        lambda item: (
            item.market.country_code,
            (item.market.region or "").casefold(),
            (item.market.city or "").casefold(),
            item.market.postal_code or "",
        ),
    )


def _confidence(score: int) -> str:
    if score >= 5:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def _identity_from_gbp(
    *,
    candidate: GbpCandidate,
    site_url: str,
    signals: SiteSignals,
    preferred_market: TargetMarket | None,
) -> BusinessIdentityCandidate | None:
    market = candidate.market or preferred_market
    operating_model = (
        signals.operating_models[0].value
        if signals.operating_models
        else candidate.operating_model
    )
    if market is None or operating_model not in {"storefront", "service_area", "hybrid"}:
        return None

    reasons: list[str] = []
    score = 0
    if _domains_match(candidate.website_url, site_url):
        score += 3
        reasons.append("website domain matched")
    if signals.names and _names_match(candidate.business_name, signals.names[0].value):
        score += 2
        reasons.append("business name matched")
    if signals.phones and _phones_match(candidate.phone, signals.phones[0].value):
        score += 2
        reasons.append("phone number matched")
    if _markets_match(candidate.market, preferred_market):
        score += 2
        reasons.append("target market matched")
    if candidate.public_gbp_url:
        reasons.append("public GBP profile identified")
    if not reasons:
        reasons.append("public GBP candidate requires identity confirmation")

    confidence = _confidence(score)
    return BusinessIdentityCandidate(
        business=BusinessIdentity(
            business_name=candidate.business_name,
            site_url=site_url,
            normalized_domain=_normalized_domain(site_url),
            operating_model=operating_model,
            primary_location=market,
            public_gbp_url=candidate.public_gbp_url,
        ),
        confidence=confidence,
        match_reasons=reasons,
        requires_confirmation=confidence != "high",
    )


def _website_identity(
    *,
    site_url: str,
    signals: SiteSignals,
    preferred_market: TargetMarket | None,
) -> BusinessIdentityCandidate | None:
    if not signals.names or preferred_market is None or not signals.operating_models:
        return None
    name = signals.names[0]
    operating_model = signals.operating_models[0]
    confidence = "high" if name.confidence == operating_model.confidence == "high" else "medium"
    return BusinessIdentityCandidate(
        business=BusinessIdentity(
            business_name=name.value,
            site_url=site_url,
            normalized_domain=_normalized_domain(site_url),
            operating_model=operating_model.value,
            primary_location=preferred_market,
            public_gbp_url=signals.gbp_url,
        ),
        confidence=confidence,
        match_reasons=[
            f"business name observed from {name.source.replace('_', ' ')}",
            "complete target market available",
            f"operating model observed from {operating_model.source.replace('_', ' ')}",
        ],
        requires_confirmation=confidence != "high",
    )


def _dedupe_models(values, key):
    result = []
    seen = set()
    for value in values:
        normalized = key(value)
        if normalized not in seen:
            seen.add(normalized)
            result.append(value)
    return tuple(result)


def build_candidates(
    *,
    request: PreflightRequest,
    normalized_site_url: str,
    signals: SiteSignals,
    gbp_result: GbpLookupResult,
) -> CandidateSet:
    """Build only complete frozen-contract candidates from observed facts."""

    services = _text_candidates(request, signals, gbp_result)
    markets = _market_candidates(request, signals, gbp_result)
    preferred_market = markets[0].market if markets else None

    identities = [
        identity
        for candidate in gbp_result.candidates
        if (identity := _identity_from_gbp(
            candidate=candidate,
            site_url=normalized_site_url,
            signals=signals,
            preferred_market=preferred_market,
        )) is not None
    ]
    if not identities:
        website_identity = _website_identity(
            site_url=normalized_site_url,
            signals=signals,
            preferred_market=preferred_market,
        )
        if website_identity is not None:
            identities.append(website_identity)

    identities_tuple = _dedupe_models(
        identities,
        lambda item: (
            item.business.business_name.casefold(),
            item.business.primary_location.display_name.casefold(),
            str(item.business.public_gbp_url or ""),
        ),
    )
    if len(identities_tuple) > 1:
        identities_tuple = tuple(
            item.model_copy(update={"requires_confirmation": True})
            for item in identities_tuple
        )
    return CandidateSet(identities_tuple, services, markets)
