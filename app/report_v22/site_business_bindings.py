"""Validate the complete customer-site wrapper before parsing any page."""
from urllib.parse import urlsplit

from app.collectors.site_inventory_urls import SiteScope
from app.jobs_v22.digest import request_digest
from app.security_v22.urls import normalize_site_url
from app.report_v22.evidence_bindings import host
from app.report_v22.site_business_errors import require


def in_scope(url, scope):
    try:
        normalize_site_url(str(url))
        return scope.contains_host(urlsplit(str(url)).hostname or "")
    except ValueError:
        return False


def validate_binding(request):
    context, source = request.context, request.source
    scope = SiteScope.from_root(str(context.site_url))
    if source is None:
        return scope, "no_snapshot"
    b, p = source.binding, source.payload
    require(b.case_id == context.case_id and b.source_type == "site" and b.schema_version == p.schema_version)
    require(p.root_url == context.site_url and p.canonical_host == host(p.root_url))
    require(p.started_at <= p.completed_at == b.fetched_at <= context.evaluated_at)
    require(request_digest(p) == b.payload_checksum, "CHECKSUM_MISMATCH")
    require(len(p.pages) <= request.limits.max_inventory_pages and len(p.selected_pages) <= request.limits.max_deep_pages, "LIMIT_EXCEEDED")
    pages = {str(page.url): page for page in p.pages}
    for page in p.pages:
        require(in_scope(page.url, scope))
    for selected in p.selected_pages:
        page = pages.get(str(selected.url))
        require(page is not None and page.check_status == "checked")
        deep = selected.deep_snapshot
        if deep is not None:
            require(deep.url == selected.url and deep.page_type == selected.page_type and deep.crawl_depth == selected.crawl_depth)
            require(p.started_at <= deep.collected_at <= p.completed_at)
    if b.expires_at is not None and b.expires_at <= context.evaluated_at or b.health_status == "expired":
        return scope, "source_expired"
    if b.health_status not in {"healthy", "not_checked"}:
        return scope, "source_unavailable"
    if b.identity_match_status == "mismatch":
        return scope, "source_identity_mismatch"
    return scope, None


def page_reason(page, deep, scope):
    if page.check_status != "checked":
        return "page_not_checked"
    if deep is None:
        return "deep_snapshot_missing"
    if not 200 <= deep.status_code < 300:
        return "http_error"
    if deep.content_type.split(";", 1)[0].strip().casefold() not in {"text/html", "application/xhtml+xml"}:
        return "content_unsupported"
    if not in_scope(deep.final_url, scope):
        return "out_of_scope_redirect"
    if not deep.html.strip():
        return "html_missing"
    return None
