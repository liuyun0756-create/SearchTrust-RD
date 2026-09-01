"""Select website candidates inside a user-confirmed entity boundary only."""
from collections import defaultdict
from urllib.parse import urlsplit

from app.report_v22.evidence_bindings import host
from app.report_v22.findings_errors import FindingsError
from app.report_v22.models import BusinessIdentity
from app.report_v22.site_business_models import FIELD_NAMES, SiteBusinessCandidate, SiteBusinessFactsResult
from app.report_v22.site_gbp_alignment_models import (
    CandidateEligibility,
    SiteBusinessSelectionResult,
    SiteFieldInspection,
    SiteGbpAlignmentLimits,
)
from app.report_v22.site_gbp_comparators import (
    compare_addresses,
    compare_business_names,
    compare_phones,
    normalize_address,
    normalize_business_name,
    normalize_phone,
    normalize_service_area,
    target_region,
)

CORE = {"home", "contact", "about", "location"}
BUSINESS_CORE = CORE | {"service_index", "service_detail", "service_area"}
REQUIRED = {
    "business_name": CORE,
    "phone": CORE,
    "address": CORE,
    "service_area": CORE | {"service_area"},
}


def _candidate_key(candidate: SiteBusinessCandidate, identity: BusinessIdentity) -> str | None:
    country = identity.primary_location.country_code
    if candidate.field == "business_name":
        return normalize_business_name(candidate.scalar_value, country).normalized_key
    if candidate.field == "phone":
        return normalize_phone(candidate.scalar_value, country).normalized_key
    if candidate.field == "address":
        value = candidate.components or candidate.scalar_value
        return normalize_address(value, country).normalized_key
    return normalize_service_area(
        candidate.scalar_value,
        country,
        region=target_region(identity.primary_location.display_name, identity.primary_location.region, country),
    ).normalized_key


def _external(candidate: SiteBusinessCandidate, identity: BusinessIdentity) -> bool:
    for value in (candidate.declared_url, candidate.declared_id):
        if not value or value.startswith("#"):
            continue
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"}:
            return host(value) != identity.normalized_domain
        if parsed.scheme:
            return True
    return False


def _market_anchor(candidate: SiteBusinessCandidate, identity: BusinessIdentity) -> bool:
    target = identity.primary_location
    region_code = target_region(target.display_name, target.region, target.country_code)
    values = [target.city, target.display_name]
    anchors = {
        normalize_service_area(value, target.country_code, region=region_code).normalized_key
        for value in values if value
    }
    if candidate.field == "service_area":
        key = normalize_service_area(candidate.scalar_value, target.country_code, region=region_code).normalized_key
        return bool(key and key in anchors)
    if candidate.field == "address":
        locality = candidate.components.get("addressLocality")
        region = candidate.components.get("addressRegion")
        city_match = locality and normalize_service_area(locality, target.country_code, region=target.region).normalized_key in anchors
        region_match = not target.region or not region or region.casefold() == target.region.casefold()
        return bool(city_match and region_match)
    return False


def _group_anchors(candidates: list[SiteBusinessCandidate], identity: BusinessIdentity) -> dict[str, str]:
    groups: dict[str, list[SiteBusinessCandidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.entity_key:
            groups[candidate.entity_key].append(candidate)
    anchored = {}
    for key, group in groups.items():
        name = any(
            item.field == "business_name"
            and compare_business_names(item.scalar_value, identity.business_name, identity.primary_location.country_code).state
            in {"exact_match", "semantic_match"}
            for item in group
        )
        market = any(_market_anchor(item, identity) for item in group)
        if name:
            anchored[key] = "noncore_name_anchor"
        elif market:
            anchored[key] = "noncore_market_anchor"
    return anchored


def _inspection(facts: SiteBusinessFactsResult, field: str) -> SiteFieldInspection:
    required = [page for page in facts.pages if page.page_type in REQUIRED[field]]
    has_home = any(page.page_type == "home" for page in required)
    checked = []
    invalid = False
    complete = facts.source_status.state == "ready" and has_home
    limitations = []
    for page in required:
        url = page.final_url or page.requested_url
        diagnostics = [item for item in page.diagnostics if field in item.fields and item.scope in {"availability", "extraction"}]
        invalid = invalid or any(item.code == "invalid_field_value" for item in diagnostics)
        blockers = [item for item in diagnostics if item.code != "invalid_field_value"]
        page_complete = page.status not in {"not_checked", "parse_failed"} and not blockers
        if page_complete:
            checked.append(url)
        else:
            complete = False
            limitations.extend(f"{field}:{item.code}" for item in blockers)
            if page.status in {"not_checked", "parse_failed"}:
                limitations.append(f"{field}:page_{page.status}")
    if not has_home:
        limitations.append(f"{field}:home_not_checked")
    return SiteFieldInspection(field=field, complete=complete,
                               required_urls=[page.final_url or page.requested_url for page in required],
                               checked_urls=checked, invalid_value_observed=invalid,
                               limitations=limitations)


def select_site_business_candidates(
    facts: SiteBusinessFactsResult,
    identity: BusinessIdentity,
    limits: SiteGbpAlignmentLimits | None = None,
) -> SiteBusinessSelectionResult:
    limits = limits or SiteGbpAlignmentLimits()
    if str(facts.site_url) != str(identity.site_url) or host(identity.site_url) != identity.normalized_domain:
        raise FindingsError("BINDING_INVALID")
    if len(facts.candidates) > limits.max_candidates:
        raise FindingsError("LIMIT_EXCEEDED")
    pages = {str(page.requested_url): page for page in facts.pages}
    candidates = sorted(facts.candidates, key=lambda item: item.candidate_id)
    anchors = _group_anchors(candidates, identity)
    preliminary: dict[str, tuple[str, str]] = {}
    for candidate in candidates:
        page = pages.get(str(candidate.requested_url))
        if page is None:
            raise FindingsError("REFERENCE_INVALID")
        if candidate.source_kind == "jsonld_property":
            if candidate.ownership_status != "declared_entity":
                preliminary[candidate.candidate_id] = (
                    "excluded" if _external(candidate, identity) else "unresolved",
                    "external_entity_hint" if _external(candidate, identity) else "ownership_unresolved",
                )
            elif page.page_type in BUSINESS_CORE:
                preliminary[candidate.candidate_id] = ("eligible", "core_declared_entity")
            elif candidate.entity_key in anchors:
                preliminary[candidate.candidate_id] = ("eligible", anchors[candidate.entity_key])
            else:
                preliminary[candidate.candidate_id] = ("unresolved", "noncore_anchor_missing")
        elif candidate.source_kind == "dom_label" and page.page_type in BUSINESS_CORE:
            preliminary[candidate.candidate_id] = ("eligible", "explicit_label_core")

    structured = [
        candidate for candidate in candidates
        if candidate.source_kind == "jsonld_property"
        and preliminary.get(candidate.candidate_id, (None,))[0] == "eligible"
    ]
    repeated: dict[tuple[str, str], set[str]] = defaultdict(set)
    repeated_core: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = _candidate_key(candidate, identity)
        page = pages[str(candidate.requested_url)]
        if key:
            repeated[(candidate.field, key)].add(str(candidate.final_url))
            if page.page_type in CORE:
                repeated_core.add((candidate.field, key))

    eligibility = []
    reference_count = 0
    for candidate in candidates:
        state_reason = preliminary.get(candidate.candidate_id)
        if state_reason is None:
            corroborated = False
            if candidate.field == "phone":
                corroborated = any(
                    item.field == "phone"
                    and compare_phones(candidate.scalar_value, item.scalar_value, identity.primary_location.country_code).state
                    in {"exact_match", "semantic_match"}
                    for item in structured
                )
            elif candidate.field == "address":
                corroborated = any(
                    item.field == "address"
                    and compare_addresses(candidate.components or candidate.scalar_value,
                                          item.components or item.scalar_value,
                                          identity.primary_location.country_code).state
                    in {"exact_match", "semantic_match"}
                    for item in structured
                )
            key = _candidate_key(candidate, identity)
            repeated_ok = bool(key and len(repeated[(candidate.field, key)]) >= 2
                               and (candidate.field, key) in repeated_core)
            if corroborated:
                state_reason = ("eligible", "structured_value_corroboration")
            elif repeated_ok:
                state_reason = ("eligible", "repeated_across_final_pages")
            else:
                state_reason = ("unresolved", "page_not_eligible")
        reference_count += len(candidate.evidence_ids)
        if reference_count > limits.max_evidence_references:
            raise FindingsError("LIMIT_EXCEEDED")
        eligibility.append(CandidateEligibility(
            candidate_id=candidate.candidate_id,
            field=candidate.field,
            state=state_reason[0],
            reasons=[state_reason[1]],
            entity_key=candidate.entity_key,
            requested_url=candidate.requested_url,
            final_url=candidate.final_url,
            evidence_ids=candidate.evidence_ids,
        ))
    return SiteBusinessSelectionResult(
        source_state=facts.source_status.state,
        eligibility=eligibility,
        inspections=[_inspection(facts, field) for field in FIELD_NAMES],
    )
