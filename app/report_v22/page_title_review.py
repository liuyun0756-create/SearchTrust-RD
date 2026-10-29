"""A narrow, conditional title-review contribution over frozen page observations.

This product heuristic detects a literal service-label difference, not a bad
title, keyword requirement, ranking cause, or measured conversion problem.
"""
import hashlib
import re
from urllib.parse import unquote, urlsplit

from app.report_v22.findings_view import EvidenceView

RULE = "v22_capability.service_title_review"
GUIDANCE = "https://developers.google.com/search/docs/appearance/title-link"
LIMIT = "A service-label difference is a title-review prompt, not proof that the title is inaccurate or that rankings or enquiries are affected. Confirm the intended page and wording before editing."


def _terms(value):
    words = re.findall(r"[a-z][a-z0-9]*", value.casefold())
    return {word[:-1] if word.endswith("s") and not word.endswith("ss") else word for word in words}


def _page(value):
    try:
        url = urlsplit(str(value))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return None
        return (url.hostname.lower().removeprefix("www."), url.port or (443 if url.scheme == "https" else 80),
                url.path.rstrip("/") or "/", url.query)
    except (ValueError, AttributeError):
        return None


def extend_report(raw, manifest, request, result):
    """Add candidates before the existing priority selection; never publish edits."""
    service = manifest.business.primary_service
    # Other writing systems need a separately validated matcher. Do not guess.
    if not service.isascii() or not request.business_identity:
        return
    terms = _terms(service)
    if len(terms) < 2:
        return
    view = EvidenceView(request.evidence_input, result.evidence_result)
    if not view.available("site"):
        return
    source = view.sources["site"]
    inventory = source.payload
    host = _page(manifest.business.site_url)
    confirmed = {_page(url) for url in manifest.business.target_pages} - {None}
    selected = {str(page.url): page for page in inventory.selected_pages}
    candidates = {}
    for index, page in enumerate(inventory.pages):
        key = _page(page.final_url)
        if (not key or not host or key[:2] != host[:2] or page.check_status != "checked"
                or page.status_code is None or not 200 <= page.status_code < 300
                or not page.content_type or "text/html" not in page.content_type.lower()
                or not page.title or not page.title.strip() or not page.h1 or not page.h1.strip()):
            continue
        if any("noindex" in re.split(r"[,\s]+", token.casefold()) for token in page.meta_robots):
            continue
        if confirmed:
            if key not in confirmed:
                continue
        elif _terms(unquote(key[2].rsplit("/", 1)[-1])) != terms:
            continue
        deep_page = selected.get(str(page.url))
        deep = deep_page.deep_snapshot if deep_page and deep_page.deep_analyzed else None
        if (not deep or not 200 <= deep.status_code < 300 or _page(deep.final_url) != key
                or "text/html" not in deep.content_type.lower()
                or (page.canonical_url is not None and _page(page.canonical_url) != key)
                or deep.title != page.title or deep.h1 != page.h1
                or not terms <= _terms(page.h1) or not terms <= _terms(deep.text)
                or terms <= _terms(page.title)):
            continue
        prefix = f"/payload/pages/{index}"
        title_refs = view.refs(source.binding.snapshot_id, f"{prefix}/title")
        heading_refs = view.refs(source.binding.snapshot_id, f"{prefix}/h1")
        if not title_refs or not heading_refs:
            continue
        candidates.setdefault(key, (page, sorted(set(title_refs + heading_refs))))
    if not confirmed and len(candidates) > 1:
        raw["limitations"].append("Several saved pages match the service label. Confirm the intended target before a page-title review is proposed.")
        return
    existing_titles = {_page(url) for action in raw["actions"] if any(m["kind"] == "title_brief" for m in action["materials"])
                       for url in action["target"].split("\n")}
    evidence = {item["id"]: item for item in raw["evidence"]}
    ref = next(ref for ref in manifest.snapshots if ref.source_kind == "site")
    for key, (page, identifiers) in sorted(candidates.items()):
        if len(raw["findings"]) >= 50:
            break
        if key in existing_titles:
            continue
        target = str(page.final_url)
        for identifier in identifiers:
            item, trace = view.items[identifier], view.traces[identifier]
            evidence[identifier] = dict(id=identifier, snapshot_id=item.snapshot_id, source_kind="site",
                object=target, field_path="\n".join(trace.origin_paths), observed_at=ref.fetched_at,
                value=item.original_value, unit=trace.selector.unit, comparison_scope=ref.scope, limitations=item.limitations)
        suffix = hashlib.sha256((str(source.binding.snapshot_id) + "|" + target).encode()).hexdigest()[:20]
        finding_id, action_id = f"fn-page-title-{suffix}", f"ac-page-title-{suffix}"
        raw["findings"].append(dict(id=finding_id, rule_id=RULE, rule_version="1.0.0", classification="fact",
            severity="low", confidence="medium", target=target,
            observed_fact=f"The checked page title is {page.title!r}; its H1 is {page.h1!r}. The service label {service!r} is represented in the H1 but not fully in the title under the literal word check.",
            observed_at=ref.fetched_at, comparison_scope="Saved title and H1 on this page only",
            judgment="Confirm this is the intended service page, then review whether its title should describe that service more explicitly. Different wording may be valid.",
            interpretation="needs_verification", impact_path="A more descriptive title may clarify the page's subject to a searcher; no ranking or enquiry effect is established.",
            evidence_ids=identifiers, limitations=[LIMIT]))
        draft = f"{service} | {request.business_identity.business_name}"
        body = (f"Page: {target}\nCurrent title: {page.title}\nCurrent H1: {page.h1}\n"
                f"Draft for SEO approval: {draft}\n\n"
                "First confirm that this page should represent the service. Keep the current title if its wording better describes the intended offer. "
                "Otherwise adapt the draft to the approved service and brand. Add a location, opening hours or promises only after those business facts are confirmed. "
                "This is a title review, not a request to rebuild the page or change its existing contact route.\n"
                f"Title guidance: {GUIDANCE}")
        raw["actions"].append(dict(id=action_id, title="Review the service-page title", target=target, finding_ids=[finding_id],
            steps=["Confirm the page's intended service and whether the existing title already describes it adequately.",
                   f"Review the saved title and H1 on {target}; compare the draft with the approved service and brand.",
                   "Keep the current wording or approve a revision. Publish only agreed wording; retain unrelated page content and contact details.",
                   "After an agreed edit, capture the same page and check that its title matches the approved wording."],
            completion=["The SEO records the intended page and a keep-or-revise decision.",
                        "If a revision is approved and published, a new same-page observation confirms the agreed title."],
            review="Check the approved title on the same URL. Any later search change needs comparable observations and does not by itself establish causation.",
            dependencies=[], materials=[dict(id=f"{action_id}-material", kind="title_brief", title="Service-page title review",
                target=target, body=body, readiness="requires_confirmation",
                confirmation_items=["Confirm that this is the intended service page.", "Approve the service description and brand wording before publication."],
                evidence_ids=identifiers)]))
        if sum(f["rule_id"] == RULE for f in raw["findings"]) >= 3:
            break
    if any(f["rule_id"] == RULE for f in raw["findings"]):
        raw["findings"] = [f for f in raw["findings"] if f["id"] != "fn-public-assessment"]
        raw["evidence"] = [evidence[key] for key in sorted(evidence)]
        raw["limitations"].append(LIMIT)
        raw["action_state"] = "actions_supported"
        if raw["summary"].startswith("No action is proposed"):
            lead = next(f for f in raw["findings"] if f["rule_id"] == RULE)
            raw["summary"] = f"{lead['observed_fact']} {lead['judgment']}"
