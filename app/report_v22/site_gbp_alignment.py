"""Build deterministic field alignment and dedicated missing-state evidence."""
from collections import defaultdict
from datetime import timedelta

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.evidence_identity import evidence_id, selector_path, stable_key
from app.report_v22.evidence_models import (
    EvidenceBuildContext,
    EvidenceBuildResult,
    EvidenceObservation,
    EvidenceSelector,
    EvidenceSourceTrace,
    PublicGbpEvidenceSource,
    SiteEvidenceSource,
)
from app.report_v22.findings_errors import FindingsError
from app.report_v22.models import BusinessIdentity, EvidenceItem, SourceLocator
from app.report_v22.site_business_models import FIELD_NAMES, SiteBusinessCandidate, SiteBusinessFactsResult
from app.report_v22.site_business_selection import REQUIRED
from app.report_v22.site_gbp_alignment_models import (
    ALIGNMENT_VERSION,
    AlignmentPair,
    SiteBusinessSelectionResult,
    SiteGbpAlignmentBuildResult,
    SiteGbpAlignmentLimits,
    SiteGbpAlignmentResult,
    SiteGbpFieldResult,
)
from app.report_v22.site_gbp_comparators import (
    compare_addresses,
    compare_business_names,
    compare_phones,
    compare_service_areas,
    normalize_service_area,
    target_region,
)

_GBP_FIELD = {
    "business_name": "business_name",
    "address": "address",
    "phone": "phone",
    "service_area": "service_areas",
}
_NOT_APPLICABLE = {
    "storefront": {"service_area"},
    "service_area": {"address"},
    "hybrid": set(),
}
_COVERAGE_NOTE = (
    "This evidence records a missing field in the saved, approved sample after required checks; "
    "it is not a business value or proof that the field is absent from the entire internet."
)


def _validate_identity(context: EvidenceBuildContext, identity: BusinessIdentity,
                       site_source: SiteEvidenceSource | None,
                       public_source: PublicGbpEvidenceSource | None,
                       facts: SiteBusinessFactsResult) -> None:
    reference = context.customer_public_gbp
    if (identity.site_url != context.site_url
            or identity.primary_location != context.target_market
            or identity.normalized_domain != (site_source.payload.canonical_host if site_source else identity.normalized_domain)
            or facts.case_id != context.case_id
            or facts.site_url != context.site_url):
        raise FindingsError("BINDING_INVALID")
    if identity.public_gbp_url is not None:
        if reference is None or identity.public_gbp_url != reference.public_gbp_url:
            raise FindingsError("BINDING_INVALID")
    if site_source is not None and facts.source_payload_checksum != site_source.binding.payload_checksum:
        raise FindingsError("CHECKSUM_MISMATCH")
    if public_source is not None:
        if reference is None or public_source.payload.subject_reference_checksum != request_digest(reference):
            raise FindingsError("CHECKSUM_MISMATCH")


def _summary_eligible(evidence: EvidenceBuildResult, snapshot_id) -> bool:
    return any(item.snapshot_id == snapshot_id and item.business_eligible for item in evidence.source_summaries)


def _gbp_items(evidence: EvidenceBuildResult, snapshot_id, field: str) -> list[tuple[object, str]]:
    items = {item.evidence_id: item for item in evidence.evidence_index}
    selected = []
    for trace in evidence.source_traces:
        if (trace.snapshot_id == snapshot_id
                and trace.selector.category == "public_gbp_field"
                and trace.selector.field == field):
            item = items.get(trace.evidence_id)
            if item is None or item.source_type != "gbp":
                raise FindingsError("REFERENCE_INVALID")
            selected.append((item.original_value, item.evidence_id))
    return sorted(selected, key=lambda pair: pair[1])


def _coverage_paths(side: str, field: str, site_source: SiteEvidenceSource,
                    public_source: PublicGbpEvidenceSource) -> list[str]:
    if side == "gbp":
        return [f"/payload/record/fields/{_GBP_FIELD[field]}/state"]
    paths = [
        f"/payload/pages/{index}"
        for index, page in enumerate(site_source.payload.pages)
        if page.page_type in REQUIRED[field]
    ]
    paths.extend(
        f"/payload/selected_pages/{index}/deep_snapshot/html"
        for index, page in enumerate(site_source.payload.selected_pages)
        if page.page_type in REQUIRED[field] and page.deep_snapshot is not None
    )
    if not paths:
        raise FindingsError("REFERENCE_INVALID")
    return sorted(set(paths))


def _coverage(side: str, field: str, identity: BusinessIdentity,
              site_source: SiteEvidenceSource, public_source: PublicGbpEvidenceSource,
              inspection) -> tuple[EvidenceItem, EvidenceSourceTrace]:
    source = site_source if side == "site" else public_source
    state = f"{side}_missing"
    context = [
        ALIGNMENT_VERSION,
        field,
        side,
        str(source.binding.snapshot_id),
        "site_business_extraction_v1",
        "site_business_selection_v1",
        state,
        *[str(url) for url in inspection.required_urls],
    ]
    selector = EvidenceSelector(
        category="coverage",
        record_key=stable_key(context),
        record_context=context,
        field=field,
    )
    paths = _coverage_paths(side, field, site_source, public_source)
    locator = SourceLocator(
        url=(identity.site_url if side == "site"
             else public_source.payload.record.observed_public_gbp_url
             or identity.public_gbp_url)
    )
    observation = EvidenceObservation(
        snapshot_id=source.binding.snapshot_id,
        source_type="coverage",
        selector=selector,
        source_locator=locator,
        original_value=state,
        normalized_value=state,
        collected_at=source.payload.completed_at,
        confidence="low",
        health_status="healthy",
        limitations=[_COVERAGE_NOTE],
        origin_paths=paths,
        gap_reason="empty",
    )
    identifier = evidence_id(observation)
    item = EvidenceItem(
        evidence_id=identifier,
        snapshot_id=source.binding.snapshot_id,
        source_type="coverage",
        source_locator=locator.model_copy(update={"field_path": selector_path(selector)}),
        original_value=state,
        normalized_value=state,
        collected_at=source.payload.completed_at,
        confidence="low",
        health_status="healthy",
        limitations=[_COVERAGE_NOTE],
    )
    trace = EvidenceSourceTrace(
        evidence_id=identifier,
        snapshot_id=source.binding.snapshot_id,
        selector=selector,
        origin_paths=paths,
    )
    return item, trace


def _pair(candidate: SiteBusinessCandidate, gbp_value, gbp_id: str, country: str):
    if candidate.field == "business_name":
        result = compare_business_names(candidate.scalar_value, gbp_value, country)
    elif candidate.field == "phone":
        result = compare_phones(candidate.scalar_value, gbp_value, country)
    elif candidate.field == "address":
        result = compare_addresses(candidate.components or candidate.scalar_value, gbp_value, country)
    else:
        raise FindingsError("REFERENCE_INVALID")
    return result, AlignmentPair(
        site_evidence_ids=candidate.evidence_ids,
        gbp_evidence_ids=[gbp_id],
        state=result.state,
        site_value=result.left,
        gbp_value=result.right,
        compared_components=result.compared_components,
        omitted_components=result.omitted_components,
    )


def _not_checked(field, reason, *, unresolved=(), urls=(), limitations=()):
    return SiteGbpFieldResult(field=field, state="not_checked", not_checked_reason=reason,
                              unresolved_candidate_ids=list(unresolved), urls=list(urls),
                              limitations=list(limitations))


def build_site_gbp_alignment(
    *,
    context: EvidenceBuildContext,
    identity: BusinessIdentity,
    facts: SiteBusinessFactsResult,
    selection: SiteBusinessSelectionResult,
    site_source: SiteEvidenceSource | None,
    public_source: PublicGbpEvidenceSource | None,
    evidence: EvidenceBuildResult,
    limits: SiteGbpAlignmentLimits | None = None,
) -> SiteGbpAlignmentBuildResult:
    limits = limits or SiteGbpAlignmentLimits()
    _validate_identity(context, identity, site_source, public_source, facts)
    candidates = {item.candidate_id: item for item in facts.candidates}
    eligible = defaultdict(list)
    unresolved = defaultdict(list)
    urls = defaultdict(set)
    for item in selection.eligibility:
        candidate = candidates.get(item.candidate_id)
        if candidate is None or candidate.field != item.field or candidate.evidence_ids != item.evidence_ids:
            raise FindingsError("REFERENCE_INVALID")
        if item.state == "eligible":
            eligible[item.field].append(candidate)
            urls[item.field].add(candidate.final_url)
        elif item.state == "unresolved":
            unresolved[item.field].append(candidate.candidate_id)
    inspections = {item.field: item for item in selection.inspections}
    if set(inspections) != set(FIELD_NAMES):
        raise FindingsError("REFERENCE_INVALID")
    results = []
    coverage_items, coverage_traces = {}, {}
    site_ready = site_source is not None and selection.source_state == "ready" and _summary_eligible(evidence, site_source.binding.snapshot_id)
    gbp_ready = public_source is not None and _summary_eligible(evidence, public_source.binding.snapshot_id)
    time_comparable = bool(site_ready and gbp_ready and abs(site_source.payload.completed_at - public_source.payload.completed_at)
                           <= timedelta(days=limits.max_comparison_age_days))

    for field in FIELD_NAMES:
        inspection = inspections[field]
        field_urls = sorted(urls[field], key=str)
        if field in _NOT_APPLICABLE[identity.operating_model]:
            gbp_ids = [] if public_source is None else [key for _, key in _gbp_items(evidence, public_source.binding.snapshot_id, _GBP_FIELD[field])]
            results.append(SiteGbpFieldResult(
                field=field,
                state="not_applicable",
                site_evidence_ids=sorted({key for candidate in eligible[field] for key in candidate.evidence_ids}),
                gbp_evidence_ids=gbp_ids,
                unresolved_candidate_ids=unresolved[field],
                urls=field_urls,
            ))
            continue
        if site_source is None or public_source is None:
            results.append(_not_checked(field, "source_missing", unresolved=unresolved[field], urls=field_urls))
            continue
        if not site_ready or not gbp_ready:
            results.append(_not_checked(field, "source_ineligible", unresolved=unresolved[field], urls=field_urls))
            continue
        if not time_comparable:
            results.append(_not_checked(field, "comparison_time_gap", unresolved=unresolved[field], urls=field_urls))
            continue
        if not inspection.complete:
            results.append(_not_checked(field, "site_content_not_checked", unresolved=unresolved[field],
                                        urls=field_urls, limitations=inspection.limitations))
            continue
        if not eligible[field] and unresolved[field]:
            results.append(_not_checked(field, "identity_unresolved", unresolved=unresolved[field], urls=field_urls))
            continue

        wrapper = getattr(public_source.payload.record.fields, _GBP_FIELD[field])
        gbp_observed = wrapper.state == "observed"
        gbp_values = _gbp_items(evidence, public_source.binding.snapshot_id, _GBP_FIELD[field]) if gbp_observed else []
        site_values = list(eligible[field])
        if field == "phone":
            site_values = [candidate for candidate in site_values
                           if compare_phones(candidate.scalar_value, candidate.scalar_value,
                                             identity.primary_location.country_code).state != "incomparable"]
            gbp_values = [(value, key) for value, key in gbp_values
                          if compare_phones(value, value, identity.primary_location.country_code).state != "incomparable"]
        site_missing = not site_values
        gbp_missing = not gbp_values
        if site_missing or gbp_missing:
            state = "both_missing" if site_missing and gbp_missing else "site_missing" if site_missing else "gbp_missing"
            result = SiteGbpFieldResult(
                field=field,
                state=state,
                site_evidence_ids=sorted({key for candidate in site_values for key in candidate.evidence_ids}),
                gbp_evidence_ids=sorted(key for _, key in gbp_values),
                unresolved_candidate_ids=unresolved[field],
                urls=field_urls,
                limitations=inspection.limitations,
            )
            for side in (["site", "gbp"] if state == "both_missing" else ["site"] if state == "site_missing" else ["gbp"]):
                item, trace = _coverage(side, field, identity, site_source, public_source, inspection)
                coverage_items[item.evidence_id] = item
                coverage_traces[trace.evidence_id] = trace
                result.coverage_evidence_ids.append(item.evidence_id)
            result.coverage_evidence_ids.sort()
            results.append(result)
            continue

        if field == "service_area":
            region_code = target_region(identity.primary_location.display_name,
                                        identity.primary_location.region,
                                        identity.primary_location.country_code)
            site_scalar = [candidate.scalar_value for candidate in site_values]
            gbp_scalar = [value for value, _ in gbp_values]
            compared = compare_service_areas(site_scalar, gbp_scalar, identity.primary_location.country_code,
                                             region=region_code)
            pairs = []
            for candidate in site_values:
                site_norm = normalize_service_area(candidate.scalar_value, identity.primary_location.country_code,
                                                   region=region_code)
                for value, gbp_id in gbp_values:
                    gbp_norm = normalize_service_area(value, identity.primary_location.country_code,
                                                      region=region_code)
                    if site_norm.normalized_key == gbp_norm.normalized_key:
                        pairs.append(AlignmentPair(site_evidence_ids=candidate.evidence_ids,
                                                   gbp_evidence_ids=[gbp_id], state="exact_match",
                                                   site_value=site_norm, gbp_value=gbp_norm))
            matched_site = {key for pair in pairs for key in pair.site_evidence_ids}
            matched_gbp = {key for pair in pairs for key in pair.gbp_evidence_ids}
            results.append(SiteGbpFieldResult(
                field=field,
                state=compared.state,
                site_evidence_ids=sorted({key for candidate in site_values for key in candidate.evidence_ids}),
                gbp_evidence_ids=sorted(key for _, key in gbp_values),
                matched_pairs=pairs,
                unmatched_site_ids=sorted({key for candidate in site_values for key in candidate.evidence_ids} - matched_site),
                unmatched_gbp_ids=sorted({key for _, key in gbp_values} - matched_gbp),
                unresolved_candidate_ids=unresolved[field],
                urls=field_urls,
            ))
            continue

        if len(site_values) * len(gbp_values) > limits.max_pair_comparisons:
            raise FindingsError("LIMIT_EXCEEDED")
        pairs, matches, incomparable = [], [], []
        for candidate in site_values:
            for value, gbp_id in gbp_values:
                compared, pair = _pair(candidate, value, gbp_id, identity.primary_location.country_code)
                pairs.append(pair)
                if compared.state in {"exact_match", "semantic_match"}:
                    matches.append(pair)
                elif compared.state == "incomparable":
                    incomparable.append(pair)
        if matches:
            state = "exact_match" if any(pair.state == "exact_match" for pair in matches) else "semantic_match"
        elif pairs and len(incomparable) == len(pairs):
            results.append(_not_checked(field, "comparator_unsupported", unresolved=unresolved[field], urls=field_urls))
            continue
        else:
            state = "mismatch"
        matched_site = {key for pair in matches for key in pair.site_evidence_ids}
        matched_gbp = {key for pair in matches for key in pair.gbp_evidence_ids}
        all_site = {key for candidate in site_values for key in candidate.evidence_ids}
        all_gbp = {key for _, key in gbp_values}
        results.append(SiteGbpFieldResult(
            field=field,
            state=state,
            site_evidence_ids=sorted(all_site),
            gbp_evidence_ids=sorted(all_gbp),
            matched_pairs=matches,
            unmatched_site_ids=sorted(all_site - matched_site),
            unmatched_gbp_ids=sorted(all_gbp - matched_gbp),
            incomparable_site_ids=sorted({key for pair in incomparable for key in pair.site_evidence_ids}),
            unresolved_candidate_ids=unresolved[field],
            urls=field_urls,
        ))

    extra_items = {item.evidence_id: item for item in facts.evidence_index}
    extra_traces = {item.evidence_id: item for item in facts.source_traces}
    for key, item in coverage_items.items():
        if key in extra_items and extra_items[key] != item:
            raise FindingsError("ID_CONFLICT")
        extra_items[key] = item
        extra_traces[key] = coverage_traces[key]
    if (len(extra_items) > limits.max_merged_evidence
            or len(coverage_items) > limits.max_coverage_evidence):
        raise FindingsError("LIMIT_EXCEEDED")
    alignment = SiteGbpAlignmentResult(
        case_id=context.case_id,
        site_snapshot_id=site_source.binding.snapshot_id if site_source else None,
        public_gbp_snapshot_id=public_source.binding.snapshot_id if public_source else None,
        site_payload_checksum=site_source.binding.payload_checksum if site_source else None,
        public_gbp_payload_checksum=public_source.binding.payload_checksum if public_source else None,
        identity_summary_checksum=request_digest(identity),
        eligibility=selection.eligibility,
        inspections=selection.inspections,
        fields=results,
    )
    output = SiteGbpAlignmentBuildResult(
        alignment=alignment,
        evidence_index=[extra_items[key] for key in sorted(extra_items)],
        source_traces=[extra_traces[key] for key in sorted(extra_traces)],
    )
    if len(canonical_json_bytes(output)) > limits.max_output_bytes:
        raise FindingsError("LIMIT_EXCEEDED")
    return output


def verify_site_gbp_alignment(result: SiteGbpAlignmentBuildResult, **inputs) -> None:
    """Rebuild the complete internal result from bound sources and compare it."""
    expected = build_site_gbp_alignment(**inputs)
    if canonical_json_bytes(result) != canonical_json_bytes(expected):
        raise FindingsError("REFERENCE_INVALID")
