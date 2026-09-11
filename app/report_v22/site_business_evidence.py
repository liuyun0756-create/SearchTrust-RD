"""Independent candidate evidence and exact source replay; old builders stay unchanged."""
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.evidence import resolve_origin
from app.report_v22.evidence_adapters.common import LIMITATIONS_OVERFLOW
from app.report_v22.evidence_identity import evidence_id, selector_path, stable_key
from app.report_v22.evidence_models import EvidenceObservation, EvidenceSelector, EvidenceSourceTrace
from app.report_v22.models import EvidenceItem, SourceLocator
from app.report_v22.site_business_errors import require
from app.report_v22.site_business_html import visible_text
from app.report_v22.site_business_models import CandidateDraft, SiteBusinessCandidate, VERSION

DECLARATION_NOTE = "These values are declarations found in saved customer-site pages; they do not establish the confirmed customer entity, a primary location, or GBP alignment."
EXTRACTION_NOTE = "Extraction is limited to supported rules in saved HTML; rendered visibility and complete website coverage have not been verified."
FIXED_NOTES = (DECLARATION_NOTE, EXTRACTION_NOTE)


def value_group(candidate):
    return request_digest({"scalar_value": candidate.scalar_value, "components": candidate.components})


def context(candidate, component=None):
    return [VERSION, str(candidate.requested_url), str(candidate.final_url), candidate.origins[0].decoded_html_checksum,
            candidate.entity_key, candidate.source_kind, candidate.field, value_group(candidate), component]


def candidate_id(candidate):
    return "sf_" + stable_key({"version": VERSION, "snapshot_id": str(candidate.snapshot_id), "context": context(candidate)})


def make_candidate(draft, source, requested_url, deep, notes):
    values = dict(**draft.model_dump(mode="python"), snapshot_id=source.binding.snapshot_id, requested_url=requested_url,
                  final_url=deep.final_url, collected_at=deep.collected_at, candidate_id="sf_" + "0" * 64)
    values["limitations"] = sorted(set([*draft.limitations, *notes, *FIXED_NOTES]))
    candidate = SiteBusinessCandidate(**values)
    return candidate.model_copy(update={"candidate_id": candidate_id(candidate)})


def limited_notes(notes):
    notes = sorted(set([*FIXED_NOTES, *notes]))
    if len(notes) <= 20:
        return notes
    other = [note for note in notes if note not in {*FIXED_NOTES, LIMITATIONS_OVERFLOW}][:17]
    return sorted([*FIXED_NOTES, *other, LIMITATIONS_OVERFLOW])


def make_evidence(candidate):
    yield from _expected_evidence(candidate)


def _expected_evidence(candidate):
    values = candidate.components.items() if candidate.components else [(None, candidate.scalar_value)]
    for component, value in values:
        record_context = context(candidate, component)
        selector = EvidenceSelector(category="site_business_field", record_key=stable_key(record_context),
                                    record_context=record_context, field=candidate.field)
        origins = [o for o in candidate.origins if o.component == component]
        require(bool(origins), "REFERENCE_INVALID")
        locator = SourceLocator(url=candidate.final_url)
        observation = EvidenceObservation(snapshot_id=candidate.snapshot_id, source_type="site", selector=selector,
            source_locator=locator, original_value=value, normalized_value=value, collected_at=candidate.collected_at,
            confidence="low", health_status="healthy", limitations=candidate.limitations,
            origin_paths=sorted({o.origin_path for o in origins}))
        # Strictly checked candidate strings must survive model whitespace defaults.
        observation = observation.model_copy(update={"original_value": value, "normalized_value": value})
        identifier = evidence_id(observation)
        item = EvidenceItem(evidence_id=identifier, snapshot_id=candidate.snapshot_id, source_type="site",
            source_locator=locator.model_copy(update={"field_path": selector_path(selector)}), original_value=value,
            normalized_value=value, collected_at=candidate.collected_at, confidence="low", health_status="healthy",
            limitations=limited_notes(candidate.limitations)).model_copy(update={"original_value": value, "normalized_value": value})
        yield item, EvidenceSourceTrace(evidence_id=identifier, snapshot_id=candidate.snapshot_id, selector=selector,
                                       origin_paths=observation.origin_paths)


def draft_fingerprint(draft):
    return canonical_json_bytes(draft.model_dump(mode="json", exclude={"origins", "limitations"}))


def verify_candidate(candidate, source_json, work):
    require(candidate.candidate_id == candidate_id(candidate), "REFERENCE_INVALID")
    require(candidate.requested_url == work["requested_url"] and candidate.final_url == work["deep"].final_url
            and candidate.collected_at == work["deep"].collected_at, "REFERENCE_INVALID")
    raw = CandidateDraft.model_validate(candidate.model_dump(mode="python", include=set(CandidateDraft.model_fields)))
    fingerprint = draft_fingerprint(raw)
    tree = work["tree"]
    for origin in candidate.origins:
        proof_key = canonical_json_bytes(origin)
        require(fingerprint in work["proofs"].get(proof_key, set()), "REFERENCE_INVALID")
        html = resolve_origin(source_json, origin.origin_path)
        require(html == tree.text and request_digest({"html": html}) == origin.decoded_html_checksum, "REFERENCE_INVALID")
        require(0 <= origin.start < origin.end <= len(html), "REFERENCE_INVALID")
        require(origin.excerpt == html[origin.start:min(origin.end, origin.start + 360)]
                and origin.excerpt_truncated == (origin.end - origin.start > 360), "REFERENCE_INVALID")
        if origin.kind == "jsonld":
            document = work["documents"].get((origin.start, origin.end))
            require(document is not None, "REFERENCE_INVALID")
            value = resolve_origin(document, origin.json_pointer)
        else:
            try:
                node = tree.at_path(origin.element_path)
            except (IndexError, TypeError):
                require(False, "REFERENCE_INVALID")
            require(node.start == origin.start and node.end == origin.end and node.closed and not node.bad and not node.hidden, "REFERENCE_INVALID")
            value = (node.attrs.get("href") or "")[4:] if origin.attribute == "href" else visible_text(node)
        expected = candidate.components[origin.component] if origin.component is not None else candidate.scalar_value
        require(type(value) is str and value == expected, "REFERENCE_INVALID")


def verify_result(result, request, works, expected_ids):
    candidates = {c.candidate_id: c for c in result.candidates}
    require(len(candidates) == len(result.candidates) and set(candidates) == expected_ids, "REFERENCE_INVALID")
    items = {item.evidence_id: item for item in result.evidence_index}
    traces = {trace.evidence_id: trace for trace in result.source_traces}
    require(len(items) == len(result.evidence_index) and len(traces) == len(result.source_traces) and items.keys() == traces.keys(), "REFERENCE_INVALID")
    used = set()
    source_json = request.source.model_dump(mode="json") if request.source else None
    for candidate in result.candidates:
        require(request.source is not None and candidate.snapshot_id == request.source.binding.snapshot_id, "REFERENCE_INVALID")
        work = works.get(str(candidate.requested_url))
        require(work is not None, "REFERENCE_INVALID")
        verify_candidate(candidate, source_json, work)
        expected = dict((item.evidence_id, (item, trace)) for item, trace in _expected_evidence(candidate))
        require(candidate.evidence_ids == sorted(expected), "REFERENCE_INVALID")
        for key, (item, trace) in expected.items():
            require(items.get(key) == item and traces.get(key) == trace, "REFERENCE_INVALID")
        used.update(expected)
    require(used == set(items), "REFERENCE_INVALID")
    for page in result.pages:
        for field, status in page.fields.items():
            expected = sorted(c.candidate_id for c in result.candidates if c.requested_url == page.requested_url and c.field == field)
            require(status.candidate_ids == expected, "REFERENCE_INVALID")
