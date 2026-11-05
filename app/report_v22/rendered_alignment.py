"""Optional rendered observations only corroborate previously unobserved fields.

The static facts and alignment are never edited in place. Candidate extraction
and origin verification reuse the v6 parser; this overlay runs only under v7.
"""
from datetime import timedelta
import hashlib

from app.collectors.site_inventory_urls import SiteScope, canonicalize_site_resource_url
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.evidence_bindings import require, eligibility
from app.report_v22.site_business_evidence import (make_candidate, make_evidence, verify_candidate, draft_fingerprint)
from app.report_v22.site_business_html import ParseBudget, parse_html, dom_candidates
from app.report_v22.site_business_jsonld import jsonld_candidates
from app.report_v22.site_business_models import TEXT_PATTERN_VERSION
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_gbp_alignment import _gbp_items, _text_alignment, _pair

from app.collectors.site_render_models import RENDER_NOTE
PARSE_NOTE = "Contact details could not be checked in the additional page response."


def validate_rendered_binding(source, inputs):
    payload = source.payload
    static = [s for s in inputs.sources if s.kind == 'site']
    require(len(static) == 1 and payload.static_payload_checksum == static[0].binding.payload_checksum)
    require(payload.root_url == inputs.context.site_url)
    scope = SiteScope.from_root(str(payload.root_url))
    for page in payload.pages:
        require(canonicalize_site_resource_url(str(page.url), scope=scope) == str(page.url))
        if page.deep_snapshot:
            deep = page.deep_snapshot
            raw = deep.html.encode('utf-8')
            require(bool(canonicalize_site_resource_url(str(deep.final_url), scope=scope)))
            require(200 <= deep.status_code < 300 and bool(deep.html.strip()))
            require(deep.response_bytes == len(raw) and deep.content_checksum == 'sha256:' + hashlib.sha256(raw).hexdigest())


def rendered_candidates(source, *, limits, phone, address, country):
    """Return independently bound candidates, with exact HTML-origin replay."""
    budget = ParseBudget(limits)
    scope = SiteScope.from_root(str(source.payload.root_url))
    candidates, items, traces, notes = {}, {}, {}, []
    source_json = source.model_dump(mode='json')
    for index, page in enumerate(source.payload.pages):
        deep = page.deep_snapshot
        if deep is None:
            continue
        try:
            tree = parse_html(deep.html, f'/payload/pages/{index}/deep_snapshot/html', budget)
            structured, diagnostics, documents = jsonld_candidates(tree, page.url, deep.final_url, scope)
            dom, dom_notes = dom_candidates(tree, text_patterns=True, gbp_phone=phone, gbp_address=address, country=country)
            drafts = [d for d in [*structured, *dom] if d.field in {'phone', 'address', 'opening_hours'}]
            if len(drafts) > limits.max_candidates_per_page:
                raise ValueError()
            work = dict(tree=tree, documents=documents, deep=deep, requested_url=page.url, proofs={})
            for draft in drafts:
                for origin in draft.origins:
                    work['proofs'].setdefault(canonical_json_bytes(origin), set()).add(draft_fingerprint(draft))
            for draft in drafts:
                candidate = make_candidate(draft, source, page.url, deep, [RENDER_NOTE], version=TEXT_PATTERN_VERSION)
                verify_candidate(candidate, source_json, work, version=TEXT_PATTERN_VERSION)
                records = list(make_evidence(candidate, version=TEXT_PATTERN_VERSION))
                candidate.evidence_ids = sorted(item.evidence_id for item, _ in records)
                candidates[candidate.candidate_id] = candidate
                for item, trace in records:
                    items[item.evidence_id], traces[item.evidence_id] = item, trace
            if diagnostics or dom_notes:
                notes.append(PARSE_NOTE)
        except (SiteBusinessError, ValueError, TypeError):
            notes.append(PARSE_NOTE)
    if len(candidates) > limits.max_candidates or len(items) > limits.max_evidence:
        return [], [], [], [PARSE_NOTE]
    return ([candidates[k] for k in sorted(candidates)], [items[k] for k in sorted(items)],
            [traces[k] for k in sorted(traces)], sorted(set(notes)))


def supplement_alignment(extra, *, facts, request, evidence):
    inputs = request.evidence_input
    rendered = next((s for s in inputs.sources if s.kind == 'rendered'), None)
    gbp = next((s for s in inputs.sources if s.kind == 'public_gbp'), None)
    if rendered is None or gbp is None or eligibility(rendered, inputs.context.evaluated_at) or eligibility(gbp, inputs.context.evaluated_at):
        return extra
    limits = request.site_gbp_alignment_limits
    if abs(rendered.payload.completed_at - gbp.payload.completed_at) > timedelta(days=limits.max_comparison_age_days):
        return extra
    fields = gbp.payload.record.fields
    phone = fields.phone.value if fields.phone.state == 'observed' else None
    address = fields.address.value if fields.address.state == 'observed' else None
    from app.report_v22.business_identity_v2 import BusinessIdentityV2
    country = (request.business_identity.country_code if isinstance(request.business_identity, BusinessIdentityV2)
               else request.business_identity.primary_location.country_code)
    if country is None:
        return extra
    candidates, items, traces, notes = rendered_candidates(rendered, limits=request.site_business_limits,
                                                          phone=phone, address=address, country=country)
    updated = []
    for field in extra.alignment.fields:
        # Reviewed static missing states are exposed as not_checked. They never
        # establish absence; candidates still take precedence even if unresolved.
        supplement = (field.field in {'phone', 'address'}
                      and (field.state == 'not_checked' and field.not_checked_reason == 'site_content_not_checked'
                           or field.state in {'site_missing', 'both_missing', 'gbp_missing'})
                      and not any(c.field == field.field for c in facts.candidates))
        if not supplement:
            updated.append(field)
            continue
        wrapper = getattr(fields, field.field)
        values = _gbp_items(evidence, gbp.binding.snapshot_id, field.field) if wrapper.state == 'observed' else []
        selected = [c for c in candidates if c.field == field.field]
        # Text and tel declarations follow the same positive-only rule.
        scalar = [c for c in selected if not c.components]
        result = _text_alignment(field.field, scalar, values, country, limits)
        pairs = list(result.matched_pairs)
        for candidate in selected:
            if not candidate.components:
                continue
            for value, identifier in values:
                compared, pair = _pair(candidate, value, identifier, country)
                if compared.state in {'exact_match', 'semantic_match'}:
                    pairs.append(pair)
        if pairs:
            result = result.model_copy(update=dict(state='semantic_match' if any(p.compared_components != ['postal'] for p in pairs) else 'partial_match',
                not_checked_reason=None, matched_pairs=pairs,
                site_evidence_ids=sorted({key for p in pairs for key in p.site_evidence_ids}),
                gbp_evidence_ids=sorted({key for p in pairs for key in p.gbp_evidence_ids})))
        result = result.model_copy(update={'limitations': sorted(set([*result.limitations, RENDER_NOTE, *notes, *rendered.payload.limitations]))})
        updated.append(result)
    return extra.model_copy(update=dict(alignment=extra.alignment.model_copy(update={'fields': updated}),
        evidence_index=[*extra.evidence_index, *items], source_traces=[*extra.source_traces, *traces]))
