"""Bounded deterministic extraction from already saved, same-entity pages."""
import re
from app.jobs_v22.digest import request_digest
from app.report_v22.business_identity_v2 import ModeEvidence, OperatingModeAssessment
from app.report_v22.site_business_html import ParseBudget, parse_html
from app.report_v22.site_business_models import SiteBusinessLimits
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.opening_hours import text_visible, HIDDEN_STYLE
from app.report_v22.evidence_bindings import host, eligibility

PATTERNS = {
    "visiting": re.compile(r"\b(?:visit (?:our|the) (?:showroom|store|shop)|customers (?:can|may) visit|walk[- ]ins? welcome|customer[- ]facing showroom)\b", re.I),
    "onsite": re.compile(r"\b(?:on[- ]site (?:engineers|service|installation)|(?:we|our (?:technicians|engineers|team)) (?:come|travel) to (?:your|the customer)|rush out to you|service trucks|in[- ]home (?:repairs?|service)|(?:we (?:repair|install|work)|our (?:team|technicians) (?:work|repair|install))[^.!?]{0,70}(?:at|in) your (?:home|property))\b", re.I),
    "meeting": re.compile(r"\bmeeting (?:areas?|locations?|points?)\b", re.I),
    "auxiliary": re.compile(r"\b(?:service areas?|areas? we serv(?:e|ed)|trading address|office address|our locations|multiple branches)\b", re.I),
}
NEGATIVE = re.compile(r"\b(?:not|never|no longer|do not|don't|cannot|can't)\b", re.I)
THIRD_PARTY = re.compile(r"\b(?:testimonial|review|case study|our client|our customer|privacy policy)\b", re.I)


def extract_mode_evidence(html, *, url, domain, checksum, path, budget):
    if host(url) != domain:
        return [], ["page_entity_unresolved"]
    try:
        tree = parse_html(html, path, budget)
    except (SiteBusinessError, ValueError):
        return [], ["page_resource_or_parse_limit"]
    rows, seen = [], set()
    for node in tree.nodes:
        if node.tag not in {"p", "li", "div", "span", "h1", "h2", "h3", "h4", "dd"} or not node.closed or node.bad:
            continue
        ancestor, hidden, third = node, False, False
        while ancestor is not None:
            hidden |= ancestor.hidden or bool(HIDDEN_STYLE.search(ancestor.attrs.get("style") or ""))
            third |= bool(THIRD_PARTY.search(" ".join(str(ancestor.attrs.get(k) or "") for k in ("class", "id"))))
            ancestor = ancestor.parent
        text = " ".join(text_visible(node).split())
        if hidden or third or not text or len(text) > 1000 or THIRD_PARTY.search(text) or NEGATIVE.search(text):
            continue
        for kind, pattern in PATTERNS.items():
            if not pattern.search(text):
                continue
            if kind == "meeting" and not re.search(r"after booking|confirmed after|vary|varies|different|map pin", text, re.I):
                continue
            if any(pattern.search(" ".join(text_visible(child).split())) for child in node.elements):
                continue
            if (kind, text) in seen:
                continue
            seen.add((kind, text))
            rows.append(ModeEvidence(kind=kind, quote=text, url=url,
                selector=f"{path}#chars={node.start}:{node.end}", payload_checksum=checksum))
            if len(rows) == 100:
                return rows, ["evidence_limit"]
    return rows, []


def inspect_operating_model(request):
    """Never reads truth or performs a request. Unavailable pages remain unknown."""
    inputs = request.evidence_input
    identity = request.business_identity
    budget = ParseBudget(request.site_business_limits)
    rows, notes = [], []
    for source in inputs.sources:
        if source.kind != "site" or eligibility(source, inputs.context.evaluated_at):
            continue
        for i, page in enumerate(source.payload.selected_pages):
            deep = page.deep_snapshot
            if not deep or not 200 <= deep.status_code < 300:
                notes.append("page_content_not_observed")
                continue
            if page.page_type not in {"home", "about", "contact", "service_index", "service_detail", "service_area", "location", "faq"}:
                continue
            found, limits = extract_mode_evidence(deep.html, url=str(deep.final_url), domain=identity.normalized_domain,
                checksum=source.binding.payload_checksum, path=f"/payload/selected_pages/{i}/deep_snapshot/html", budget=budget)
            rows.extend(found); notes.extend(limits)
    gbp_field = None
    for source in inputs.sources:
        if source.kind == "public_gbp" and not eligibility(source, inputs.context.evaluated_at):
            wrapper = source.payload.record.fields.service_area_business
            gbp_field = wrapper.model_dump(mode="json")
            if wrapper.state == "observed" and type(wrapper.value) is bool and wrapper.value is True:
                rows.append(ModeEvidence(kind="gbp_service_area", quote="service_area_business=true",
                    url=source.payload.record.observed_public_gbp_url or source.payload.request_target.public_gbp_url,
                    selector="/payload/record/fields/service_area_business/value", payload_checksum=source.binding.payload_checksum))
    if len(rows) > 100:
        notes.append("evidence_limit")
    assessment = assess_mode(rows[:100], gbp_field=gbp_field)
    if notes and assessment.value is not None:
        assessment = assessment.model_copy(update={"value": None, "state": "needs_confirmation",
            "conflicts": [*assessment.conflicts, "incomplete_mode_evidence"]})
    return assessment, sorted(set(notes))


def assess_mode(evidence, *, gbp_field=None, confirmation=None):
    """A provider flag is positive evidence only for a real observed boolean."""
    kinds = {row.kind for row in evidence}
    suggested, value, alternatives, conflicts = None, None, [], []
    if "meeting" in kinds:
        suggested = "meeting_point"
        if "visiting" in kinds:
            conflicts.append("meeting_and_customer_visitation")
            alternatives = ["meeting_point", "storefront", "hybrid"]
        else:
            value = suggested
    elif "visiting" in kinds and "onsite" in kinds:
        value = suggested = "hybrid"
    elif "visiting" in kinds:
        suggested = "storefront"
        alternatives = ["storefront", "hybrid"]
    elif "onsite" in kinds:
        suggested = "service_area"
        alternatives = ["service_area", "hybrid"]
    elif "auxiliary" in kinds:
        alternatives = ["service_area", "hybrid", "storefront"]
    # Callers may supply a provenanced observed boolean; false/absence is no proof of a storefront.
    positive = (isinstance(gbp_field, dict) and gbp_field.get("state") == "observed"
                and type(gbp_field.get("value")) is bool and gbp_field["value"] is True)
    source = "website" if evidence else "insufficient_evidence"
    if positive and any(row.kind == "gbp_service_area" for row in evidence):
        if "visiting" in kinds or "meeting" in kinds:
            conflicts.append("gbp_service_area_conflicts_with_page")
            value = None
        elif suggested is None:
            suggested, alternatives, source = "service_area", ["service_area", "hybrid"], "gbp"
    visiting_pages = {(str(e.url), e.payload_checksum) for e in evidence if e.kind == "visiting"}
    onsite_pages = {(str(e.url), e.payload_checksum) for e in evidence if e.kind == "onsite"}
    mixed = (visiting_pages and onsite_pages and not visiting_pages.intersection(onsite_pages))
    branches = any(re.search(r"\b(?:branches|our locations|multiple locations|different locations|\w+ branch)\b", e.quote, re.I) for e in evidence)
    if mixed or branches:
        conflicts.append("branch_context_unresolved")
        value = None
    if confirmation:
        value, source = confirmation.value, "explicit_confirmation"
    return OperatingModeAssessment(value=value, state="resolved" if value else "needs_confirmation" if suggested or alternatives or conflicts else "unresolved",
        suggested_value=suggested, alternatives=alternatives, source=source,
        evidence_refs=evidence, conflicts=conflicts, confirmation_ref=confirmation)
