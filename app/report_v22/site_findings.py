"""Observed HTTP/meta/title facts, without business-qualification conclusions."""
from collections import defaultdict
import re
from urllib.parse import urldefrag

from app.report_v22.evidence_adapters.site_counts import eligible_html
from app.report_v22.findings_common import decision
from app.report_v22.findings_models import RuleTarget
from app.report_v22.public_rule_catalog import HTTP, NOINDEX, TITLE


def evaluate(view):
    if not view.available("site"):
        for rule in (HTTP, NOINDEX, TITLE):
            yield decision(view, rule, RuleTarget(kind="site"), "not_checked", view.missing_reason("site"))
        return
    source = view.sources["site"]
    snapshot = source.binding.snapshot_id
    final_pages = defaultdict(list)
    missing_titles = False
    for index, page in enumerate(source.payload.pages):
        path = f"/payload/pages/{index}"
        target = RuleTarget(kind="page", snapshot_id=snapshot, urls=[page.url])
        if page.check_status != "checked" or page.status_code is None:
            for rule in (HTTP, NOINDEX):
                yield decision(view, rule, target, "not_checked", "field_not_observed")
            missing_titles = True
            continue
        refs = view.refs(snapshot, f"{path}/status_code", f"{path}/check_status", required=True)
        triggered = 400 <= page.status_code <= 599
        yield decision(view, HTTP, target, "triggered" if triggered else "not_triggered",
                       "condition_met" if triggered else "condition_not_met", evidence=refs,
                       statement=f"The saved response for {page.url} had HTTP status {page.status_code}.",
                       severity="high" if page.status_code >= 500 else "medium",
                       notes=["A saved HTTP error is not proof that the URL is permanently inaccessible."])
        if not eligible_html(page):
            yield decision(view, NOINDEX, target, "not_checked", "field_not_observed")
            missing_titles = True
            continue
        tokens = {token.casefold() for value in page.meta_robots for token in re.split(r"[,\s]+", value) if token}
        noindex = "noindex" in tokens
        refs = view.field_refs(snapshot, path, "status_code", "content_type", "meta_robots")
        yield decision(view, NOINDEX, target, "triggered" if noindex else "not_triggered",
                       "condition_met" if noindex else "condition_not_met", evidence=refs,
                       statement=f"The saved HTML metadata for {page.url} contains an explicit noindex token.",
                       notes=["Only saved meta_robots tokens were inspected; response headers, policy intent and actual search-engine indexing were not verified."])
        if page.title and page.title.strip():
            title = " ".join(page.title.split()).casefold()
            final = urldefrag(str(page.final_url))[0]
            refs = view.field_refs(snapshot, path, "title", "status_code", "final_url", "content_type")
            final_pages[final].append((page.url, title, refs))
        else:
            missing_titles = True

    groups = defaultdict(list)
    ambiguous = False
    for final, records in sorted(final_pages.items()):
        if len({r[1] for r in records}) != 1:
            ambiguous = True
            continue
        groups[records[0][1]].append((final, records))
    duplicate_groups = 0
    comparable = sum(len(group) for group in groups.values())
    for title, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        duplicate_groups += 1
        urls = [url for _, records in group for url, _, _ in records]
        refs = [key for _, records in group for _, _, keys in records for key in keys]
        target = RuleTarget(kind="title_group", snapshot_id=snapshot, urls=urls, title_key=title)
        yield decision(view, TITLE, target, "triggered", "condition_met", evidence=refs,
                       statement=f"The checked sample contains {len(group)} distinct final URLs with the same whitespace/case-normalized title: {title!r}.",
                       notes=["Repeated titles do not establish duplicate body content or lack of independent page value."])
    # A coverage record is deliberately distinct from each actual title group.
    target = RuleTarget(kind="site", snapshot_id=snapshot)
    if ambiguous or missing_titles or comparable < 2:
        reason = "ambiguous_page_observations" if ambiguous else "field_not_observed" if missing_titles else "insufficient_sample"
        yield decision(view, TITLE, target, "not_checked", reason)
    elif duplicate_groups == 0:
        refs = [key for records in final_pages.values() for _, _, keys in records for key in keys]
        yield decision(view, TITLE, target, "not_triggered", "condition_not_met", evidence=refs)
