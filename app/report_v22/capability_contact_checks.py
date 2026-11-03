"""Read-only display records from the already verified alignment, public v8 only."""
from app.report_v22.capability_finding_status import alignment_status
from app.report_v22.rendered_alignment import RENDER_NOTE


def contact_checks_from_alignment(alignment, request, evidence):
    if alignment is None:
        return []
    sources = {s.binding.snapshot_id: s.kind for s in request.evidence_input.sources}
    public = next((s for s in request.evidence_input.sources if s.kind == "public_gbp"), None)
    checks = []
    for field in alignment.fields:
        pairs = field.matched_pairs
        ids = sorted(set(field.site_evidence_ids + field.gbp_evidence_ids + field.coverage_evidence_ids
            + [key for pair in pairs for key in pair.site_evidence_ids + pair.gbp_evidence_ids]))
        # Scope is copied from the matched pairs, never re-extracted or expanded.
        compared = sorted({part for pair in pairs for part in pair.compared_components})
        omitted = sorted({part for pair in pairs for part in pair.omitted_components})
        rendered = any(sources[evidence[key].snapshot_id] == "rendered" for key in ids)
        basis = ("rendered" if rendered or RENDER_NOTE in field.limitations else
                 "text_pattern" if any(p.site_value.version == "text_pattern_v1" for p in pairs) else "structured")
        wrapper = getattr(public.payload.record.fields,
            "service_areas" if field.field == "service_area" else field.field) if public else None
        result = {"exact_match": "match", "semantic_match": "match",
                  "partial_match": "partial_match", "mismatch": "mismatch"}.get(field.state, "none")
        checks.append(dict(field=field.field, result=result, status=alignment_status(field.state,
            gbp_field_state=wrapper.state if wrapper else None), basis=basis,
            compared_components=compared, omitted_components=omitted, evidence_ids=ids))
    return checks
