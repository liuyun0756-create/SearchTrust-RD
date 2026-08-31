"""Offline customer-site declarations, explicitly separate from business judgments."""
from pydantic import BaseModel, ValidationError

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.site_business_bindings import page_reason, validate_binding
from app.report_v22.site_business_errors import SiteBusinessError, require
from app.report_v22 import site_business_evidence as evidence
from app.report_v22.site_business_html import ParseBudget, diagnostic, dom_candidates, parse_html
from app.report_v22.site_business_jsonld import jsonld_candidates
from app.report_v22.site_business_models import (
    CandidateDiagnostic, CandidateFieldStatus, CandidatePage, CandidateSourceStatus, FIELD_NAMES,
    SiteBusinessFactsInput, SiteBusinessFactsResult,
)


def unique(values):
    registry = {canonical_json_bytes(value): value for value in values}
    return [registry[key] for key in sorted(registry)]


def unpack_models(value):
    """Never let nested model instances inside a dict skip strict validation."""
    if isinstance(value, BaseModel):
        return unpack_models(value.model_dump(mode="python", warnings=False))
    if isinstance(value, dict):
        return {key: unpack_models(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unpack_models(item) for item in value]
    if isinstance(value, tuple):
        return tuple(unpack_models(item) for item in value)
    return value


def field_states(candidates, diagnostics, checked):
    fields = {}
    for field in FIELD_NAMES:
        selected = [candidate for candidate in candidates if candidate.field == field]
        owners = {candidate.ownership_status for candidate in selected}
        incomplete = not checked or any(field in d.fields and d.scope in {"extraction", "availability"} for d in diagnostics)
        fields[field] = CandidateFieldStatus(
            observation_status="observed" if selected else "not_checked" if incomplete else "not_observed",
            ownership_status="not_applicable" if not owners else next(iter(owners)) if len(owners) == 1 else "mixed",
            candidate_ids=sorted({c.candidate_id for c in selected}))
    return fields


def build_site_business_facts(value: SiteBusinessFactsInput | dict) -> SiteBusinessFactsResult:
    try:
        reject_nonfinite(value)
        raw = unpack_models(value)
        request = SiteBusinessFactsInput.model_validate(raw)
        limits = request.limits
        require(len(canonical_json_bytes(request)) <= limits.max_input_bytes, "LIMIT_EXCEEDED")
        scope, reason = validate_binding(request)
        source, context = request.source, request.context
        pages, candidates, works, items, traces = [], {}, {}, {}, {}
        notes = sorted(set([*evidence.FIXED_NOTES, *(source.payload.limitations if source else [])]))
        result = SiteBusinessFactsResult(case_id=context.case_id, site_url=context.site_url, evaluated_at=context.evaluated_at,
            source_snapshot_id=source.binding.snapshot_id if source else None,
            source_payload_checksum=source.binding.payload_checksum if source else None,
            source_status=CandidateSourceStatus(state="missing" if source is None else "ineligible" if reason else "ready", reason=reason),
            pages=[], candidates=[], evidence_index=[], source_traces=[], limitations=notes)
        retained, origin_count = len(canonical_json_bytes(result)), 0
        budget = ParseBudget(limits)
        if reason is None:
            selected = {str(s.url): (index, s.deep_snapshot) for index, s in enumerate(source.payload.selected_pages)}
            for page in sorted(source.payload.pages, key=lambda p: str(p.url)):
                position, deep = selected.get(str(page.url), (None, None))
                page_record = CandidatePage(requested_url=page.url, page_type=page.page_type,
                    inventory_check_status=page.check_status, inventory_error_code=page.error_code, status="not_checked",
                    fields=field_states([], [], False),
                    **(dict(final_url=deep.final_url, collected_at=deep.collected_at, response_status=deep.status_code,
                            content_type=deep.content_type, content_checksum=deep.content_checksum) if deep else {}))
                unavailable = page_reason(page, deep, scope)
                page_candidates = {}
                if unavailable:
                    page_record.diagnostics = [CandidateDiagnostic(code=unavailable, fields=list(FIELD_NAMES), scope="availability")]
                else:
                    path = f"/payload/selected_pages/{position}/deep_snapshot/html"
                    tree = parse_html(deep.html, path, budget)
                    structured, diagnostics, documents = jsonld_candidates(tree, page.url, deep.final_url, scope)
                    dom, dom_diagnostics = dom_candidates(tree)
                    diagnostics.extend(dom_diagnostics)
                    drafts = [*structured, *dom]
                    require(len(drafts) <= limits.max_candidates_per_page, "LIMIT_EXCEEDED")
                    if any(d.ownership_status == "unresolved" for d in drafts):
                        diagnostics.append(diagnostic(tree, "ownership_unresolved", [], scope="ownership"))
                    page_record.diagnostics = unique(diagnostics)
                    page_record.decoded_html_checksum = tree.checksum
                    errors = [d for d in diagnostics if d.scope == "extraction"]
                    page_record.status = "parse_failed" if tree.dom_failed and not drafts and not documents else "partial" if errors else "parsed"
                    work = dict(tree=tree, documents=documents, deep=deep, requested_url=page.url, proofs={})
                    works[str(page.url)] = work
                    for draft in drafts:
                        fingerprint = evidence.draft_fingerprint(draft)
                        for origin in draft.origins:
                            work["proofs"].setdefault(canonical_json_bytes(origin), set()).add(fingerprint)
                        candidate = evidence.make_candidate(draft, source, page.url, deep, notes)
                        key = candidate.candidate_id
                        if key in page_candidates:
                            previous = page_candidates[key]
                            require(evidence.draft_fingerprint(previous) == evidence.draft_fingerprint(candidate), "ID_CONFLICT")
                            previous.origins = unique([*previous.origins, *candidate.origins])
                            previous.limitations = sorted(set([*previous.limitations, *candidate.limitations]))
                        else:
                            page_candidates[key] = candidate
                        require(len(candidates) + len(page_candidates) <= limits.max_candidates, "LIMIT_EXCEEDED")
                    page_record.fields = field_states(list(page_candidates.values()), diagnostics, True)
                for key, candidate in sorted(page_candidates.items()):
                    candidate.origins = unique(candidate.origins)
                    origin_count += len(candidate.origins)
                    require(origin_count <= limits.max_origins, "LIMIT_EXCEEDED")
                    for item, trace in evidence.make_evidence(candidate):
                        require(item.evidence_id not in items or (items[item.evidence_id] == item and traces[item.evidence_id] == trace), "ID_CONFLICT")
                        if item.evidence_id not in items:
                            retained += len(canonical_json_bytes(item)) + len(canonical_json_bytes(trace))
                        items[item.evidence_id], traces[item.evidence_id] = item, trace
                        candidate.evidence_ids.append(item.evidence_id)
                        require(len(items) <= limits.max_evidence, "LIMIT_EXCEEDED")
                    candidate.evidence_ids = sorted(set(candidate.evidence_ids))
                    require(key not in candidates, "ID_CONFLICT")
                    candidates[key] = candidate
                    retained += len(canonical_json_bytes(candidate))
                    require(retained <= limits.max_output_bytes, "LIMIT_EXCEEDED")
                pages.append(page_record)
                retained += len(canonical_json_bytes(page_record))
                require(retained <= limits.max_output_bytes, "LIMIT_EXCEEDED")
        result.pages = pages
        result.candidates = [candidates[key] for key in sorted(candidates)]
        result.evidence_index = [items[key] for key in sorted(items)]
        result.source_traces = [traces[key] for key in sorted(traces)]
        result.limitations = sorted(set([*result.limitations, *(note for c in result.candidates for note in c.limitations)]))
        require(len(canonical_json_bytes(result)) <= limits.max_output_bytes, "LIMIT_EXCEEDED")
        evidence.verify_result(result, request, works, set(candidates))
        return result
    except EvidenceError:
        raise SiteBusinessError("REFERENCE_INVALID") from None
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise SiteBusinessError("INPUT_INVALID") from None
