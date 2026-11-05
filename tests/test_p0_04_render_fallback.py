import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import fakeredis.aioredis
import httpx
import pytest

from app.collectors.site_inventory_urls import SiteScope
from app.collectors.site_render_firecrawl import FirecrawlRenderAdapter, FirecrawlRenderResult
from app.core.config import Settings
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import PricingCatalog
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.site_render_stage import SiteRenderStage, static_contact_check
from public_gbp_helpers import public_source
from site_business_helpers import request
from evidence_helpers import NOW

JOB = UUID(int=404)


async def public_dns(host):
    return ['93.184.216.34']


def test_flag_defaults_off_and_contact_precedes_short_text():
    assert Settings(_env_file=None).V22_SITE_RENDER_FALLBACK_ENABLED is False
    deep = request('<p>512-555-0100</p>').source.payload.selected_pages[0].deep_snapshot
    assert static_contact_check(deep)[0] is True
    assert static_contact_check(deep)[1] < 200


@pytest.mark.parametrize('case', ['ok', 'timeout', 'error', 'oversize', 'invalid', 'empty', 'redirect', 'http_page_error'])
def test_bounded_scrape_adapter(case):
    async def run():
        seen = []
        def handler(req):
            import json
            seen.append(json.loads(req.content))
            if case == 'timeout': raise httpx.ReadTimeout('private upstream details')
            if case == 'error': return httpx.Response(500)
            if case == 'oversize': return httpx.Response(200, content=b'x'*2001)
            if case == 'invalid': return httpx.Response(200, text='not json')
            return httpx.Response(200, json={'success': True, 'data': {
                'html': '' if case == 'empty' else '<footer><p>512-555-0100</p></footer>',
                'metadata': {'statusCode': 404 if case == 'http_page_error' else 200,
                             'sourceURL': 'https://rival.test/' if case == 'redirect' else 'https://example.test/'}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = FirecrawlRenderAdapter(api_key='test-key', api_url='https://api.test/v1', enabled=True,
                max_response_bytes=2000, http_client=client, resolver=public_dns)
            result = await adapter.scrape('https://example.test/', scope=SiteScope.from_root('https://example.test/'))
        assert len(seen) == 1 and seen[0]['onlyMainContent'] is False and seen[0]['formats'] == ['html']
        assert seen[0]['proxy'] == 'basic'
        assert (result.html is not None) is (case == 'ok')
        if case != 'ok': assert result.limitation == 'render_unavailable' and result.html is None
    asyncio.run(run())


class FakeRenderer:
    available = True
    def __init__(self, fail=False): self.calls, self.fail = [], fail
    async def scrape(self, url, *, scope):
        self.calls.append(url)
        if self.fail: return FirecrawlRenderResult(limitation='render_unavailable')
        return FirecrawlRenderResult(html='<p>512-555-0100</p>', final_url=url, status_code=200)


async def setup(enabled=True, contact=False, fail=False):
    redis = fakeredis.aioredis.FakeRedis()
    cp = JobCheckpoints(redis, prefix='p004', ttl_seconds=600)
    ledger = JobCostLedger(redis, prefix='p004', job_id=JOB, ttl_seconds=600,
        job_created_at=NOW-timedelta(hours=2), pricing=PricingCatalog(), clock=lambda: NOW)
    static = request('<p>512-555-0100</p>' if contact else '<div id="app"></div>').source.payload
    business = SimpleNamespace(site_url='https://example.test/', target_pages=[])
    renderer = FakeRenderer(fail)
    stage = SiteRenderStage(adapter=renderer, enabled=enabled, clock=lambda: NOW)
    args = dict(job_id=JOB, business=business, static=static, gbp=public_source().payload,
                country='US', checkpoints=cp, cost_ledger=ledger)
    return redis, renderer, stage, args


@pytest.mark.parametrize('enabled,contact', [(False, False), (False, True), (True, True)])
def test_no_calls_when_disabled_or_static_contact_already_observed(enabled, contact):
    async def run():
        redis, renderer, stage, args = await setup(enabled, contact)
        before = canonical_json_bytes(args['static'])
        try:
            assert await stage.collect(**args) is None
            assert not renderer.calls
            assert (await args['cost_ledger'].snapshot()).root['firecrawl_attempts'] == 0
            assert canonical_json_bytes(args['static']) == before
        finally: await redis.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('fail', [False, True])
def test_render_is_independent_checkpointed_and_always_accounted(fail):
    async def run():
        redis, renderer, stage, args = await setup(fail=fail)
        before = canonical_json_bytes(args['static'])
        try:
            result = await stage.collect(**args)
            assert result.source_kind == 'rendered' and len(result.pages) == 1
            assert (result.pages[0].deep_snapshot is not None) is (not fail)
            assert bool(result.limitations) is fail
            assert await stage.collect(**args) == result
            assert len(renderer.calls) == 1
            costs = (await args['cost_ledger'].snapshot()).root
            assert costs['firecrawl_attempts'] == 1
            assert costs['firecrawl_failures' if fail else 'firecrawl_successes'] == 1
            assert (await args['cost_ledger'].state()).schema_version == 'v22_cost_ledger_v2'
            assert canonical_json_bytes(args['static']) == before
        finally: await redis.aclose()
    asyncio.run(run())


def test_only_home_and_first_customer_target_and_budget_survives_new_checkpoints():
    async def run():
        redis, renderer, stage, args = await setup()
        args['business'].target_pages = ['https://rival.test/', 'https://example.test/service', 'https://example.test/extra']
        try:
            result = await stage.collect(**args)
            assert renderer.calls == ['https://example.test/', 'https://example.test/service']
            args['checkpoints'] = JobCheckpoints(redis, prefix='p004-retry', ttl_seconds=600)
            retry = await stage.collect(**args)
            assert len(renderer.calls) == 2
            assert all(p.limitation == 'render_budget_exhausted' for p in retry.pages)
            assert (await args['cost_ledger'].snapshot()).root['firecrawl_attempts'] == 2
        finally: await redis.aclose()
    asyncio.run(run())


def rendered_source(site, html=None, *, failed=False):
    import hashlib
    from app.collectors.site_render_models import RenderedDeepPage, RenderedPage, RenderedSiteSnapshot, RENDER_FAILURE
    from app.report_v22.evidence_models import RenderedEvidenceSource
    from app.jobs_v22.digest import request_digest
    deep = None
    if not failed:
        raw = html.encode()
        original = site.payload.selected_pages[0].deep_snapshot
        deep = RenderedDeepPage.model_validate({**original.model_dump(mode='python'), 'html': html,
            'response_bytes': len(raw), 'content_checksum': 'sha256:'+hashlib.sha256(raw).hexdigest()})
    payload = RenderedSiteSnapshot(root_url=site.payload.root_url,
        static_payload_checksum=site.binding.payload_checksum, started_at=site.payload.started_at,
        completed_at=site.payload.completed_at, pages=[RenderedPage(url=site.payload.root_url,
            reason='limited_visible_text', static_text_characters=0, deep_snapshot=deep,
            limitation='render_unavailable' if failed else None)], limitations=[RENDER_FAILURE] if failed else [])
    binding = site.binding.model_copy(update=dict(snapshot_id=UUID(int=405), schema_version=payload.schema_version,
                                                  payload_checksum=request_digest(payload)))
    return RenderedEvidenceSource(binding=binding, payload=payload)


@pytest.mark.parametrize('html,expected', [
    ('<p>512-555-0100</p>', 'not_triggered'),
    ('<a href="tel:+15125550100">Call</a>', 'not_triggered'),
    ('<a href="tel:+15125550199">Tracking number</a>', 'not_checked'),
    ('<p>512-555-0199</p>', 'not_checked'),
    ('<div>No usable contact</div>', 'not_checked'),
])
def test_rendered_phone_only_corroborates_never_mismatch_or_missing(html, expected):
    from test_p0_03_text_patterns import run
    from app.report_v22.findings import build_public_findings
    _, _, _, value = run('<div id="app"></div>')
    site = next(s for s in value.evidence_input.sources if s.kind == 'site')
    before = canonical_json_bytes(site)
    source = rendered_source(site, html)
    value.evidence_input.sources.append(source)
    result = build_public_findings(value, text_patterns=True, reviewed_evidence=True, rendered_pages=True)
    phone = next(e for e in result.rule_evaluations if e.rule_id.endswith('phone_alignment'))
    assert phone.state == expected and phone.finding_id is None
    assert any('rendered page' in note for note in phone.limitations)
    if expected == 'not_triggered':
        assert phone.reason == 'semantic_match'
        assert any(e.snapshot_id == source.binding.snapshot_id for e in result.evidence_result.evidence_index if e.evidence_id in phone.evidence_ids)
    assert canonical_json_bytes(site) == before


def test_static_identity_unresolved_candidate_cannot_be_overridden_by_render():
    from test_p0_03_text_patterns import run
    from app.report_v22.findings import build_public_findings
    _, _, _, value = run('<a href="tel:+15125550199">Call</a>')
    original = build_public_findings(value, text_patterns=True, reviewed_evidence=True)
    site = next(s for s in value.evidence_input.sources if s.kind == 'site')
    value.evidence_input.sources.append(rendered_source(site, '<p>512-555-0100</p>'))
    result = build_public_findings(value, text_patterns=True, reviewed_evidence=True, rendered_pages=True)
    old = next(e for e in original.rule_evaluations if e.rule_id.endswith('phone_alignment'))
    new = next(e for e in result.rule_evaluations if e.rule_id.endswith('phone_alignment'))
    assert new == old and new.reason == 'identity_unresolved'


def versioned_report_fixture(failed=False):
    from app.report_v22.capability_public import build_public_capability_report, RENDERED_RULESET
    from app.report_v22.capability import AnalysisInputManifest
    from app.jobs_v22.digest import verified_request_digest
    from app.jobs_v22.capability_inputs import generation_recipe
    from test_v22_capability_public import inputs
    from site_business_helpers import source
    from app.jobs_v22.capability_acquisition_input import stamp
    manifest, value = inputs(business_source=source('<div id="app"></div>'))
    static = next(s for s in value.evidence_input.sources if s.kind == 'site')
    render = rendered_source(static, ' \n<p>512-555-0100</p>\n ', failed=failed)
    value.evidence_input.sources.append(render)
    raw = manifest.model_dump(mode='json')
    raw['schema_version'], raw['ruleset_version'] = 'v22_analysis_input_v2', RENDERED_RULESET
    ref = dict(raw['snapshots'][0])
    ref.update(snapshot_id=str(render.binding.snapshot_id), source_kind='rendered', schema_version=render.binding.schema_version,
        payload_checksum=render.binding.payload_checksum, fetched_at=stamp(render.binding.fetched_at))
    raw['snapshots'].append(ref)
    raw['generation_input_checksum'] = verified_request_digest(value.model_dump(mode='json'))
    raw['input_checksum'] = verified_request_digest({k:v for k,v in raw.items() if k!='input_checksum'})
    manifest = AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw))
    report = build_public_capability_report(manifest, value)
    return manifest, value, report


@pytest.mark.parametrize('failed', [False, True])
def test_versioned_report_keeps_static_source_and_generates_on_render_failure(failed):
    from app.jobs_v22.capability_inputs import generation_recipe
    manifest, value, report = versioned_report_fixture(failed)
    assert report.schema_version == '2.2.4'
    assert any(e.source_kind == 'rendered' for e in report.evidence)
    if failed:
        assert any('could not be completed' in note for note in report.limitations)
    else:
        assert next(s for s in value.evidence_input.sources if s.kind == 'rendered').payload.pages[0].deep_snapshot.html.startswith(' \n')
        assert any(e.value == '512-555-0100' and 'rendered page' in ' '.join(e.limitations) for e in report.evidence)
    assert generation_recipe(manifest, value)[2]['schema_version'] == 'v22_generation_recipe_v2'


@pytest.mark.parametrize('html,address,country,reason', [
    ('<p>123 Fixture Street</p>', '123 Fixture Street, Austin, TX 78701', 'US', 'semantic_match'),
    ('<p>SA14 6EF</p>', 'Unit 9, Parc Gelli Werdd, Llanelli SA14 6EF', 'GB', 'partial_match'),
    ('<p>987 Another Road</p>', '123 Fixture Street, Austin, TX 78701', 'US', 'site_content_not_checked'),
])
def test_rendered_address_is_positive_only_and_retains_comparison_scope(html,address,country,reason):
    from test_p0_03_text_patterns import run
    from app.report_v22.findings import build_public_findings
    _, _, _, value = run('<div id="app"></div>',address=address,country=country)
    site = next(s for s in value.evidence_input.sources if s.kind == 'site')
    value.evidence_input.sources.append(rendered_source(site, html))
    result = build_public_findings(value,text_patterns=True,reviewed_evidence=True,rendered_pages=True)
    field = next(e for e in result.rule_evaluations if e.rule_id.endswith('address_alignment'))
    assert field.reason == reason and field.state != 'triggered' and field.finding_id is None
    assert 'rendered page' in ' '.join(field.limitations)


def test_acquisition_bridge_keeps_rendered_manifest_separate_with_current_ruleset():
    from test_v22_capability_acquisition_preparation import inputs
    from app.jobs_v22.capability_acquisition_input import build_acquisition_input
    from app.report_v22.capability_public import build_public_capability_report
    from app.jobs_v22.digest import request_digest
    args,_,_ = inputs()
    old_manifest,old_value = build_acquisition_input(**args)
    before = canonical_json_bytes(build_public_capability_report(old_manifest,old_value))
    # A soft failure is a separately stored observation, not a replacement site.
    from app.collectors.site_render_models import RenderedSiteSnapshot,RenderedPage,RENDER_FAILURE
    static = args['collected'].site_inventory
    rendered = RenderedSiteSnapshot(root_url=static.root_url,static_payload_checksum=request_digest(static),
        started_at=args['now'],completed_at=args['now'],pages=[RenderedPage(url=static.root_url,
        reason='static_not_checked',static_text_characters=0,limitation='render_unavailable')],limitations=[RENDER_FAILURE])
    manifest,value = build_acquisition_input(**args,rendered=rendered)
    report = build_public_capability_report(manifest,value)
    assert manifest.schema_version == 'v22_analysis_input_v2' and manifest.ruleset_version == 'v22_capability_public_v11'
    assert report.schema_version == '2.2.6' and RENDER_FAILURE in report.limitations
    again, unchanged = build_acquisition_input(**args,rendered=None)
    assert (again,unchanged)==(old_manifest,old_value)
    assert canonical_json_bytes(build_public_capability_report(again,unchanged)) == before


def test_rendered_source_rejects_wrong_static_checksum_and_old_rule_dispatch():
    from test_p0_03_text_patterns import run
    from app.report_v22.findings import build_public_findings
    from app.report_v22.findings_errors import FindingsError
    from app.jobs_v22.digest import request_digest
    _, _, _, value = run('<div id="app"></div>')
    site = next(s for s in value.evidence_input.sources if s.kind=='site')
    rendered = rendered_source(site,'<p>512-555-0100</p>')
    value.evidence_input.sources.append(rendered)
    with pytest.raises(FindingsError):
        build_public_findings(value,text_patterns=True,reviewed_evidence=True)
    rendered.payload.static_payload_checksum = 'sha256:'+'a'*64
    rendered.binding.payload_checksum=request_digest(rendered.payload)
    with pytest.raises(FindingsError):
        build_public_findings(value,text_patterns=True,reviewed_evidence=True,rendered_pages=True)


def test_contact_on_target_with_trailing_slash_still_skips_render():
    async def run():
        redis, renderer, stage, args = await setup(contact=True)
        from app.collectors.site_inventory_models import SiteInventorySnapshot
        raw = canonical_json_bytes(args['static']).decode().replace('https://example.test/','https://example.test/city/')
        args['static'] = SiteInventorySnapshot.model_validate_json(raw)
        args['business'].site_url='https://example.test/city/'
        try:
            assert await stage.collect(**args) is None
            assert renderer.calls == []
        finally:
            await redis.aclose()
    asyncio.run(run())


def test_rendered_delivery_fixtures_match_actual_v7_generation():
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]/'search-trust/src/lib/report-capability/fixtures'
    for failed,name in [(False,'v7-rendered'),(True,'v7-render-unavailable')]:
        if not root.exists():
            pytest.skip('sibling frontend not available')
        assert (root/(name+'.json')).read_bytes()==canonical_json_bytes(versioned_report_fixture(failed)[2])
