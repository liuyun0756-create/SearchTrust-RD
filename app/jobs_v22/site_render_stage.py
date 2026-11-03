"""Customer-only, two-call render fallback after static collection and before freezing inputs."""
from datetime import datetime, timezone
import hashlib
from time import monotonic

from app.collectors.site_render_models import RenderedDeepPage
from app.collectors.site_inventory_urls import SiteScope, canonicalize_site_resource_url
from app.collectors.site_render_firecrawl import FirecrawlRenderAdapter
from app.collectors.site_render_models import RENDER_FAILURE, RenderedPage, RenderedSiteSnapshot
from app.jobs_v22.cost_ledger import CostLimitExceeded
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.site_business_html import ParseBudget, dom_candidates, parse_html
from app.report_v22.site_business_jsonld import jsonld_candidates
from app.report_v22.site_business_models import SiteBusinessLimits
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_business_text import text_visible

VISIBLE_TEXT_THRESHOLD = 200


def static_contact_check(deep, *, gbp_phone=None, gbp_address=None, country=''):
    if deep is None or not 200 <= deep.status_code < 300 or not deep.html.strip():
        return False, 0, 'static_not_checked'
    try:
        tree = parse_html(deep.html, '/payload/html', ParseBudget(SiteBusinessLimits()))
        scope = SiteScope.from_root(str(deep.final_url))
        structured, _, _ = jsonld_candidates(tree, deep.url, deep.final_url, scope)
        dom, _ = dom_candidates(tree, text_patterns=True, gbp_phone=gbp_phone, gbp_address=gbp_address, country=country)
        contact = any(c.field in {'phone', 'address', 'opening_hours'} for c in [*structured, *dom])
        length = len(' '.join(text_visible(tree.root).split()))
        return contact, length, 'limited_visible_text' if length < VISIBLE_TEXT_THRESHOLD else 'contact_not_observed'
    except (SiteBusinessError, ValueError, TypeError):
        return False, 0, 'static_not_checked'


class SiteRenderStage:
    def __init__(self, *, adapter, enabled=False, clock=None):
        self.adapter, self.enabled = adapter, enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect(self, *, job_id, business, static, gbp, country, checkpoints, cost_ledger):
        if not self.enabled:
            return None
        scope = SiteScope.from_root(business.site_url)
        urls = []
        for raw in [business.site_url, *business.target_pages]:
            url = canonicalize_site_resource_url(raw, scope=scope)
            if url and url not in urls:
                urls.append(url)
            if len(urls) == 2:
                break
        checksum = request_digest(static)
        identity = request_digest({'static': checksum, 'urls': urls, 'gbp': request_digest(gbp), 'country': country})
        key = 'site_render_v1:snapshot:' + identity[7:]

        async def operation():
            started = self.clock()
            selected = {canonicalize_site_resource_url(str(p.url), scope=scope): p.deep_snapshot for p in static.selected_pages}
            inventory = {canonicalize_site_resource_url(str(p.url), scope=scope): p for p in static.pages}
            fields = gbp.record.fields
            phone = fields.phone.value if fields.phone.state == 'observed' else None
            address = fields.address.value if fields.address.state == 'observed' else None
            pages = []
            for url in urls:
                page = inventory.get(url)
                if page and page.check_status == 'robots_disallowed':
                    continue
                deep = selected.get(url)
                contact, length, reason = static_contact_check(deep, gbp_phone=phone, gbp_address=address, country=country)
                # User-confirmed precedence: even a short page with one contact observation needs no render.
                if contact:
                    continue
                page_key = 'site_render_v1:page:' + request_digest({'basis': identity, 'url': url})[7:]

                async def scrape_page():
                    base = dict(url=url, reason=reason, static_text_characters=length)
                    if cost_ledger is None or not self.adapter.available:
                        return RenderedPage(**base, limitation='render_unavailable').model_dump(mode='json')
                    try:
                        claim = await cost_ledger.claim('firecrawl_scrape')
                    except CostLimitExceeded:
                        return RenderedPage(**base, limitation='render_budget_exhausted').model_dump(mode='json')
                    tick = monotonic()
                    try:
                        result = await self.adapter.scrape(url, scope=scope)
                    except Exception:
                        await cost_ledger.complete(claim.claim_id, outcome='failure', duration_ms=max(0,int((monotonic()-tick)*1000)))
                        return RenderedPage(**base, limitation='render_unavailable').model_dump(mode='json')
                    await cost_ledger.complete(claim.claim_id, outcome='failure' if result.limitation else 'success',
                                               duration_ms=max(0,int((monotonic()-tick)*1000)))
                    if result.limitation:
                        return RenderedPage(**base, limitation='render_unavailable').model_dump(mode='json')
                    body = result.html.encode('utf-8')
                    rendered = RenderedDeepPage(url=url, final_url=result.final_url,
                        page_type=deep.page_type if deep else page.page_type if page else 'other',
                        crawl_depth=deep.crawl_depth if deep else page.crawl_depth if page else 0,
                        collected_at=self.clock(), status_code=result.status_code, content_type='text/html',
                        response_bytes=len(body), content_checksum='sha256:'+hashlib.sha256(body).hexdigest(),
                        html=result.html, text='')
                    return RenderedPage(**base, deep_snapshot=rendered).model_dump(mode='json')

                raw = await checkpoints.run_once(job_id, page_key, scrape_page)
                pages.append(RenderedPage.model_validate_json(canonical_json_bytes(raw)))
            if not pages:
                return {'snapshot': None}
            # A resumed page retains its original collection time.
            observed = [p.deep_snapshot.collected_at for p in pages if p.deep_snapshot]
            snapshot = RenderedSiteSnapshot(root_url=static.root_url, static_payload_checksum=checksum,
                started_at=min([started, *observed]), completed_at=self.clock(), pages=pages,
                limitations=[RENDER_FAILURE] if any(p.limitation for p in pages) else [])
            return {'snapshot': snapshot.model_dump(mode='json')}

        raw = await checkpoints.run_once(job_id, key, operation)
        return RenderedSiteSnapshot.model_validate_json(canonical_json_bytes(raw['snapshot'])) if raw['snapshot'] else None


def build_site_render_stage(settings):
    return SiteRenderStage(enabled=settings.V22_SITE_RENDER_FALLBACK_ENABLED,
        adapter=FirecrawlRenderAdapter(api_key=settings.FIRECRAWL_API_KEY, api_url=settings.FIRECRAWL_API_URL,
            enabled=settings.V22_SITE_RENDER_FALLBACK_ENABLED,
            timeout_seconds=settings.V22_SITE_INVENTORY_TOTAL_TIMEOUT_SECONDS,
            max_response_bytes=settings.V22_SITE_INVENTORY_DEEP_BYTES))
