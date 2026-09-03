"""Pure deterministic competitor identity aggregation, filtering, and ranking."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from app.api.v2.models import CompetitorCandidate, DataGap
from app.collectors.serp_market_models import SerpMarketResultRecord, SerpMarketSnapshot
from app.competitors_v22.limits import COMPETITOR_MIN_COUNT
from app.competitors_v22.models import (
    COMPETITOR_DISCOVERY_CANDIDATE_LIMIT,
    CandidateAuditRecord,
    CandidateIdentityAudit,
    CandidateRankingResult,
    CandidateScore,
)
from app.competitors_v22.normalization import (
    canonical_competitor_url,
    normalize_address,
    normalize_domain,
    normalize_name,
    normalize_text,
    tokenize_relevance_text,
)
from app.report_v22.models import BusinessIdentity, TargetMarket


_BLOCKED_PLATFORM_DOMAINS = frozenset(
    {
        "angi.com",
        "angieslist.com",
        "expertise.com",
        "facebook.com",
        "homeadvisor.com",
        "houzz.com",
        "instagram.com",
        "linkedin.com",
        "mapquest.com",
        "nextdoor.com",
        "threebestrated.com",
        "thumbtack.com",
        "yellowpages.com",
        "yelp.com",
        "youtube.com",
    }
)


@dataclass(frozen=True)
class _Observation:
    record: SerpMarketResultRecord
    domain: str | None
    normalized_name: str
    normalized_address: str
    strong_ids: frozenset[str]


@dataclass(frozen=True)
class _ObservationGroup:
    observations: list[_Observation]
    ambiguous: bool = False


def _strong_ids(record: SerpMarketResultRecord) -> frozenset[str]:
    values: set[str] = set()
    for prefix, value in (
        ("place", record.provider_place_id),
        ("data", record.provider_data_id),
        ("cid", record.provider_cid),
    ):
        if value:
            values.add(f"{prefix}:{normalize_text(value)}")
    return frozenset(values)


def _observations(snapshot: SerpMarketSnapshot) -> list[_Observation]:
    return [
        _Observation(
            record=record,
            domain=record.normalized_domain or normalize_domain(str(record.url) if record.url else None),
            normalized_name=normalize_name(record.display_name),
            normalized_address=normalize_address(record.address),
            strong_ids=_strong_ids(record),
        )
        for run in snapshot.query_runs
        for record in run.results
    ]


def _should_merge(left: _Observation, right: _Observation) -> bool:
    if left.strong_ids & right.strong_ids:
        return True
    if left.strong_ids and right.strong_ids:
        return False
    if left.domain and left.domain == right.domain:
        return True
    return bool(
        left.normalized_name
        and left.normalized_address
        and left.normalized_name == right.normalized_name
        and left.normalized_address == right.normalized_address
    )


def _groups(observations: list[_Observation]) -> list[_ObservationGroup]:
    def connected_groups(items: list[_Observation], *, strong_only: bool) -> list[list[_Observation]]:
        parents = list(range(len(items)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        for left in range(len(items)):
            for right in range(left + 1, len(items)):
                should_merge = bool(items[left].strong_ids & items[right].strong_ids)
                if not strong_only:
                    should_merge = _should_merge(items[left], items[right])
                if should_merge:
                    union(left, right)
        grouped: dict[int, list[_Observation]] = {}
        for index, observation in enumerate(items):
            grouped.setdefault(find(index), []).append(observation)
        return list(grouped.values())

    strong_groups = connected_groups(
        [item for item in observations if item.strong_ids],
        strong_only=True,
    )
    weak_groups = connected_groups(
        [item for item in observations if not item.strong_ids],
        strong_only=False,
    )
    unmatched_weak: list[_ObservationGroup] = []
    for weak_group in weak_groups:
        matches = [
            strong_group
            for strong_group in strong_groups
            if any(_should_merge(weak, strong) for weak in weak_group for strong in strong_group)
        ]
        if len(matches) == 1:
            matches[0].extend(weak_group)
        else:
            # A weak organic/domain observation must never bridge two distinct
            # Google place identities that happen to share a franchise site.
            unmatched_weak.append(_ObservationGroup(weak_group, ambiguous=len(matches) > 1))
    return [
        *(_ObservationGroup(group) for group in strong_groups),
        *unmatched_weak,
    ]


def _blocked_domain(domain: str | None) -> bool:
    return bool(
        domain
        and any(domain == blocked or domain.endswith(f".{blocked}") for blocked in _BLOCKED_PLATFORM_DOMAINS)
    )


def _most_common(values: list[str]) -> str:
    counts = Counter(value for value in values if value)
    return min(counts, key=lambda value: (-counts[value], value)) if counts else ""


def _display_name(group: list[_Observation]) -> str:
    local_names = [
        item.record.display_name
        for item in group
        if item.record.result_type in {"maps", "local_pack"}
    ]
    return _most_common(local_names or [item.record.display_name for item in group])


def _identity_key(group: list[_Observation], *, domain: str | None, name: str, address: str) -> str:
    strong = sorted(identity for item in group for identity in item.strong_ids)
    if strong:
        return strong[0]
    if domain:
        return f"domain:{domain}"
    return f"name-address:{name}|{address}"


def _competitor_id(identity_key: str) -> str:
    return f"cp_{hashlib.sha256(identity_key.encode()).hexdigest()[:32]}"


def _public_gbp_url(group: list[_Observation]) -> str | None:
    cids = sorted(
        {
            item.record.provider_cid
            for item in group
            if item.record.provider_cid and item.record.provider_cid.isdecimal()
        }
    )
    return f"https://www.google.com/maps?cid={cids[0]}" if cids else None


def _client_cid(business: BusinessIdentity) -> str | None:
    if business.public_gbp_url is None:
        return None
    query = parse_qs(urlsplit(str(business.public_gbp_url)).query)
    value = query.get("cid", [None])[0]
    return value if value and value.isdecimal() else None


def _is_client_business(
    group: list[_Observation],
    *,
    business: BusinessIdentity,
    domain: str | None,
    name: str,
    address: str,
) -> bool:
    client_domain = business.normalized_domain.casefold().removeprefix("www.")
    if domain and domain == client_domain:
        return True
    client_cid = _client_cid(business)
    if client_cid and any(item.record.provider_cid == client_cid for item in group):
        return True
    if name != normalize_name(business.business_name):
        return False
    location_tokens = {
        normalize_text(value)
        for value in (
            business.primary_location.city,
            business.primary_location.region,
            business.primary_location.postal_code,
        )
        if value
    }
    return not address or any(token and token in address for token in location_tokens)


def _market_tokens(market: TargetMarket) -> set[str]:
    return tokenize_relevance_text(
        " ".join(
            value
            for value in (
                market.display_name,
                market.city,
                market.region,
                market.postal_code,
            )
            if value
        )
    )


def _business_relevance(
    group: list[_Observation],
    *,
    primary_service: str,
    queries: list[str],
) -> float:
    service_tokens = tokenize_relevance_text(primary_service)
    query_tokens = set().union(*(tokenize_relevance_text(query) for query in queries))
    haystack = set().union(
        *(
            tokenize_relevance_text(
                " ".join(
                    [
                        item.record.display_name,
                        item.record.snippet or "",
                        " ".join(item.record.categories),
                        str(item.record.url or ""),
                    ]
                )
            )
            for item in group
        )
    )
    service_score = len(service_tokens & haystack) / max(len(service_tokens), 1)
    query_score = len(query_tokens & haystack) / max(len(query_tokens), 1)
    return min(1.0, service_score * 0.75 + query_score * 0.25)


def _has_local_evidence(group: list[_Observation], market: TargetMarket) -> bool:
    if any(item.record.result_type in {"maps", "local_pack"} for item in group):
        return True
    market_tokens = _market_tokens(market)
    observed = set().union(
        *(
            tokenize_relevance_text(
                f"{item.record.address or ''} {item.record.snippet or ''} {item.record.display_name}"
            )
            for item in group
        )
    )
    return bool(market_tokens & observed)


def _rank_score(group: list[_Observation]) -> float:
    best_by_context: dict[tuple[str, str], int] = {}
    for item in group:
        key = (item.record.query.casefold(), item.record.result_type)
        best_by_context[key] = min(best_by_context.get(key, item.record.position), item.record.position)
    values = [1.0 / math.log2(position + 1) for position in best_by_context.values()]
    return sum(values) / len(values)


def _identity_confidence(group: list[_Observation], *, domain: str | None, address: str) -> float:
    has_strong = any(item.strong_ids for item in group)
    if has_strong and domain:
        return 1.0
    if domain and address:
        return 0.8
    if domain:
        return 0.6
    return 0.0


def _identity_audit(
    group: list[_Observation],
    *,
    identity_key: str,
    domain: str | None,
    name: str,
    address: str,
) -> CandidateIdentityAudit:
    return CandidateIdentityAudit(
        stable_identity_key=identity_key,
        normalized_name=name,
        normalized_domain=domain,
        normalized_address=address or None,
        provider_place_ids=sorted({item.record.provider_place_id for item in group if item.record.provider_place_id}),
        provider_data_ids=sorted({item.record.provider_data_id for item in group if item.record.provider_data_id}),
        provider_cids=sorted({item.record.provider_cid for item in group if item.record.provider_cid}),
        result_record_ids=sorted({item.record.record_id for item in group}),
        queries=sorted({item.record.query for item in group}, key=lambda value: value.casefold()),
        result_types=sorted(
            {item.record.result_type for item in group},
            key={"maps": 0, "local_pack": 1, "organic": 2}.get,
        ),
    )


def _excluded(identity: CandidateIdentityAudit, reason: str) -> CandidateAuditRecord:
    return CandidateAuditRecord(identity=identity, disposition="excluded", reason_code=reason)


def _eligible_record(
    group: list[_Observation],
    *,
    ambiguous: bool,
    business: BusinessIdentity,
    primary_service: str,
    target_market: TargetMarket,
    query_count: int,
) -> CandidateAuditRecord:
    domains = [item.domain for item in group if item.domain]
    domain = _most_common(domains) or None
    name = normalize_name(_display_name(group))
    address = _most_common([normalize_address(item.record.address) for item in group])
    identity_key = _identity_key(group, domain=domain, name=name, address=address)
    identity = _identity_audit(
        group,
        identity_key=identity_key,
        domain=domain,
        name=name,
        address=address,
    )
    if ambiguous:
        return CandidateAuditRecord(
            identity=identity,
            disposition="ambiguous",
            reason_code="ambiguous_identity",
        )
    if domain is None:
        return _excluded(identity, "missing_website")
    if _blocked_domain(domain):
        return _excluded(identity, "blocked_platform")
    if _is_client_business(group, business=business, domain=domain, name=name, address=address):
        return _excluded(identity, "client_business")
    if not _has_local_evidence(group, target_market):
        return _excluded(identity, "no_local_evidence")
    relevance = _business_relevance(group, primary_service=primary_service, queries=identity.queries)
    if relevance < 0.15:
        return _excluded(identity, "business_irrelevant")

    coverage = len(identity.queries) / query_count
    rank = _rank_score(group)
    confidence_value = _identity_confidence(group, domain=domain, address=address)
    total = coverage * 0.45 + rank * 0.30 + relevance * 0.20 + confidence_value * 0.05
    score = CandidateScore(
        query_coverage=coverage,
        rank=rank,
        business_relevance=relevance,
        identity_confidence=confidence_value,
        total=total,
    )
    if confidence_value >= 0.8 and len(identity.queries) >= 2:
        confidence = "high"
    elif confidence_value >= 0.6:
        confidence = "medium"
    else:
        confidence = "low"
    display_name = _display_name(group)
    candidate = CompetitorCandidate(
        competitor_id=_competitor_id(identity_key),
        business_name=display_name,
        website_url=canonical_competitor_url(f"https://{domain}"),
        public_gbp_url=_public_gbp_url(group),
        query_appearance_count=len(identity.queries),
        best_position=min(item.record.position for item in group),
        relevance_reason=(
            f"Appeared for {len(identity.queries)} of {query_count} confirmed queries; "
            "service, local market, and identity signals were verified."
        ),
        confidence=confidence,
    )
    return CandidateAuditRecord(
        identity=identity,
        disposition="eligible",
        reason_code="eligible",
        score=score,
        candidate=candidate,
    )


def _sort_key(record: CandidateAuditRecord) -> tuple[float, int, float, float, str]:
    assert record.score is not None and record.candidate is not None
    return (
        -record.score.total,
        -record.candidate.query_appearance_count,
        -record.score.rank,
        -record.score.business_relevance,
        record.candidate.competitor_id,
    )


def _suppress_shared_websites(records: list[CandidateAuditRecord]) -> list[CandidateAuditRecord]:
    eligible = sorted((record for record in records if record.disposition == "eligible"), key=_sort_key)
    seen_domains: set[str] = set()
    replacements: dict[str, CandidateAuditRecord] = {}
    for record in eligible:
        assert record.candidate is not None
        domain = normalize_domain(str(record.candidate.website_url))
        if domain in seen_domains:
            replacements[record.identity.stable_identity_key] = record.model_copy(
                update={
                    "disposition": "excluded",
                    "reason_code": "shared_competitor_website",
                    "candidate": None,
                }
            )
        elif domain:
            seen_domains.add(domain)
    return [replacements.get(record.identity.stable_identity_key, record) for record in records]


def rank_competitor_candidates(
    snapshot: SerpMarketSnapshot,
    *,
    business: BusinessIdentity,
    primary_service: str,
    target_market: TargetMarket,
    supplemental_website_urls: list[str] | None = None,
) -> CandidateRankingResult:
    records = [
        _eligible_record(
            grouped.observations,
            ambiguous=grouped.ambiguous,
            business=business,
            primary_service=primary_service,
            target_market=target_market,
            query_count=len(snapshot.queries),
        )
        for grouped in _groups(_observations(snapshot))
    ]
    records = _suppress_shared_websites(records)
    eligible = sorted((record for record in records if record.disposition == "eligible"), key=_sort_key)
    selected = eligible[:COMPETITOR_DISCOVERY_CANDIDATE_LIMIT]

    data_gaps: list[DataGap] = []
    supplemental_domains = {
        domain
        for value in supplemental_website_urls or []
        if (domain := normalize_domain(value)) is not None
    }
    eligible_by_domain = {
        normalize_domain(str(record.candidate.website_url)): record
        for record in eligible
        if record.candidate is not None
    }
    for domain in sorted(supplemental_domains):
        supplemental = eligible_by_domain.get(domain)
        if supplemental is None:
            data_gaps.append(
                DataGap(
                    gap_code="SUPPLEMENTAL_COMPETITOR_NOT_IN_MARKET",
                    message="A supplemental competitor was not eligible in the current market snapshot.",
                    blocking=False,
                    resolution="Adjust the confirmed queries and run competitor discovery again.",
                )
            )
            continue
        if supplemental in selected:
            continue
        replace_index = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if normalize_domain(str(selected[index].candidate.website_url)) not in supplemental_domains
            ),
            None,
        )
        if replace_index is not None:
            selected[replace_index] = supplemental
            selected.sort(key=_sort_key)

    candidates = [record.candidate for record in selected if record.candidate is not None]
    ready = len(candidates) >= COMPETITOR_MIN_COUNT
    if not ready:
        data_gaps.append(
            DataGap(
                gap_code="INSUFFICIENT_COMPETITORS",
                message="No eligible competitors were found.",
                blocking=True,
                resolution="Add at least one market-visible competitor or adjust the confirmed queries.",
            )
        )
    return CandidateRankingResult(
        candidates=candidates,
        audit_records=records,
        ready_for_confirmation=ready,
        data_gaps=data_gaps,
        limitations=[],
    )
