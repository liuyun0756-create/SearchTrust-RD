"""Offline production collector tests; live capture is deliberately not imported."""
import gzip
from pathlib import Path
import pytest
from app.collectors.site_inventory_fetcher import SiteFetchResponse, SiteFetchError
from app.collectors.site_inventory import SiteInventoryCollector
from app.collectors.site_contact_coverage import discover_contacts
from app.collectors.site_inventory_urls import SiteScope
from test_v22_site_inventory_collector import FakeFetcher, html_response, NOW

@pytest.fixture
def anyio_backend(): return 'asyncio'

def worker(fetcher):
    return SiteInventoryCollector(fetcher=fetcher,firecrawl=None,structural_max_bytes=256000,
        sitemap_max_bytes=100000,sitemap_decompressed_max_bytes=500000,sitemap_max_files=5,
        sitemap_max_depth=2,batch_size=1,resource_limits=True,sampling_quality=True,contact_coverage=True,clock=lambda:NOW)

@pytest.mark.anyio
@pytest.mark.parametrize('size', [1,5,20,21])
async def test_small_sample_reserves_contact_before_sitemap_and_within_limits(size):
    base='https://example.com/'
    links=''.join(f'<a href="/services/{i}">service</a>' for i in range(max(0,size-2)))
    links+='<a href="/contact.html">Contact Us</a>' if size>1 else ''
    pages={base:html_response(base,links),base+'contact.html':html_response(base+'contact.html','<title>Contact Us</title><meta name="robots" content="noindex"><p>Opening Hours Monday 09:00-17:00</p>')}
    pages.update({base+f'services/{i}':html_response(base+f'services/{i}','<h1>Service</h1>') for i in range(max(0,size-2))})
    fetcher=FakeFetcher(pages)
    result=await worker(fetcher).collect(site_url=base,discovery_limit=50,deep_analysis_limit=2)
    assert result.schema_version=='site_inventory_snapshot_v3'
    assert result.deep_analyzed_count<=2
    if 1<size<=20:
        assert result.selected_pages[1].page_type=='contact'
        assert result.selected_pages[1].deep_snapshot.html.endswith('</p>')
        assert 'noindex' in next(p for p in result.pages if p.page_type=='contact').meta_robots
    else: assert not any('contact_reserved' in p.selection_reasons for p in result.selected_pages)

@pytest.mark.anyio
@pytest.mark.parametrize('limit', [1,2])
async def test_limits_and_contact_failure_are_not_missing_evidence(limit):
    base='https://example.com/'
    f=FakeFetcher({base:html_response(base,'<a href="/contact">Contact</a>'),base+'contact':SiteFetchError('page_timeout','timeout')})
    result=await worker(f).collect(site_url=base,discovery_limit=limit,deep_analysis_limit=limit)
    assert result.discovered_url_count<=limit and len(result.selected_pages)<=limit
    assert any('contact_not_checked' in l for l in result.limitations)
    assert result.deep_analyzed_count==1


def test_contact_links_require_real_safe_links_not_substrings():
    links,_=discover_contacts('''<a href="/">Contact</a><a href="/reach-us">Contact Us</a><a href="/blog/contact-lenses">article</a>
      <a href="https://evil.test/contact">Contact</a><a href="/wp-login.php">Contact</a><a href="mailto:a@b.test">Contact</a>
      <a href="/contact.htm">Go</a>''',SiteScope.from_root('https://example.com/'))
    assert links==['https://example.com/reach-us','https://example.com/contact.htm']

@pytest.mark.anyio
@pytest.mark.parametrize('frozen_home',[False,True])
async def test_original_volcanoes_tail_link_and_saved_real_contact_end_to_end(frozen_home):
    # Separate current capture, NOT the frozen gold manifest; no live IO.
    base='https://www.volcanoeshikingguides.com/'
    root=(Path(__file__).parent/'fixtures/gold/volcanoes-hiking-guides/page.html').read_bytes() if frozen_home else gzip.decompress((Path(__file__).parent/'fixtures/p1-00b-contact/volcanoes/home.html.gz').read_bytes())
    contact=gzip.decompress((Path(__file__).parent/'fixtures/p1-00b-contact/volcanoes/contact.html.gz').read_bytes())
    assert root.find(b'/contact-us')>1_000_000
    class Bounded(FakeFetcher):
        async def fetch(self,url,**kw):
            self.calls.append(url)
            body=root if url==base else contact if url==base+'contact-us' else b''
            if not body: return html_response(url,'',status=404)
            bound=kw['max_bytes']
            if len(body)>bound and not kw.get('allow_prefix'):raise SiteFetchError('response_too_large','bound')
            return SiteFetchResponse(requested_url=url,final_url=url,status_code=200,content_type='text/html',body=body[:bound],truncated=len(body)>bound)
    fetcher=Bounded({})
    result=await worker(fetcher).collect(site_url=base,discovery_limit=30,deep_analysis_limit=3)
    page=next(p for p in result.selected_pages if str(p.url)==base+'contact-us')
    assert page.deep_analyzed and page.deep_snapshot.html==contact.decode().strip()
    assert fetcher.calls.count(base)==2  # prefix + exactly one bounded link scan, cached for deep use
    assert result.deep_analyzed_count==2
    if not frozen_home:
        import json
        from datetime import datetime
        from types import SimpleNamespace
        from app.report_v22.findings_models import PublicFindingsInput
        from app.report_v22.opening_hours import site_candidates,gbp_candidates,compare_candidates
        from app.report_v22.evidence_bindings import eligibility
        batch=Path(__file__).parent/'fixtures/p1-00b-contact/volcanoes'
        capture=json.loads((batch/'capture-resumed.json').read_text())
        now=datetime.fromisoformat(capture['completed_at'])
        bound=json.loads(gzip.decompress((Path(__file__).parent/'fixtures/gold/volcanoes-hiking-guides/bound-p0-10.json.gz').read_bytes()))
        request=PublicFindingsInput.model_validate_json(json.dumps(bound['request']))
        gbp=next(s for s in request.evidence_input.sources if s.kind=='public_gbp')
        rows=site_candidates(SimpleNamespace(payload=result),request.business_identity)
        assert eligibility(gbp,now) is None
        assert compare_candidates(rows,gbp_candidates(gbp))['state']=='exact_match'

@pytest.mark.anyio
@pytest.mark.parametrize('first_fails',[False,True])
async def test_second_contact_is_only_attempted_after_first_failure(first_fails):
    base='https://example.com/'
    pages={base:html_response(base,'<a href="/contact">Contact</a><a href="/get-in-touch">Get in Touch</a>'),
      base+'contact':html_response(base+'contact','<h1>Contact</h1>',status=404 if first_fails else 200),
      base+'get-in-touch':html_response(base+'get-in-touch','<h1>Get in Touch</h1>')}
    f=FakeFetcher(pages);result=await worker(f).collect(site_url=base,discovery_limit=3,deep_analysis_limit=2)
    assert (base+'get-in-touch' in f.calls)==first_fails
    assert result.deep_analyzed_count==2

@pytest.mark.anyio
async def test_robots_forbids_contact_without_calling_it():
    base='https://example.com/'
    f=FakeFetcher({base:html_response(base,'<a href="/contact">Contact</a>'),base+'robots.txt':SiteFetchResponse(requested_url=base+'robots.txt',final_url=base+'robots.txt',status_code=200,content_type='text/plain',body=b'User-agent: *\nDisallow: /contact')})
    result=await worker(f).collect(site_url=base,discovery_limit=3,deep_analysis_limit=2)
    assert base+'contact' not in f.calls
    assert any('contact_not_checked' in x for x in result.limitations)

@pytest.mark.anyio
async def test_v11_checkpoints_are_separate_and_reused_without_more_fetches():
    import fakeredis.aioredis
    from app.jobs_v22.checkpoints import JobCheckpoints
    from test_v22_site_inventory_stage import JOB_ID,RecordingFetcher,InterruptingFirecrawl,stage,prospect_request
    redis=fakeredis.aioredis.FakeRedis(decode_responses=True)
    keys=[]
    class Recording(JobCheckpoints):
        async def run_once(self, job_id, key, operation):
            keys.append(key)
            return await super().run_once(job_id,key,operation)
    checkpoints=Recording(redis,prefix='p100b',ttl_seconds=600)
    fetcher=RecordingFetcher();firecrawl=InterruptingFirecrawl();firecrawl.calls=1
    stage_=stage(fetcher,firecrawl);stage_.resource_limits=True;stage_.sampling_quality=True
    await stage_.collect(job_id=JOB_ID,request=prospect_request(),checkpoints=checkpoints)
    old={k:await redis.get(k) for k in await redis.keys('*')}
    stage_.contact_coverage=True
    v11=await stage_.collect(job_id=JOB_ID,request=prospect_request(),checkpoints=checkpoints)
    count=len(fetcher.calls)
    again=await stage_.collect(job_id=JOB_ID,request=prospect_request(),checkpoints=checkpoints)
    assert v11==again and v11.schema_version=='site_inventory_snapshot_v3' and len(fetcher.calls)==count
    assert all([await redis.get(k)==v for k,v in old.items()])
    assert any('site_inventory_v4:snapshot' in k for k in keys)
    await redis.aclose()


@pytest.mark.anyio
async def test_contact_late_in_small_sitemap_and_home_contact_role():
    base='https://example.com/'
    sitemap='<urlset>'+''.join(f'<url><loc>{base}{p}</loc></url>' for p in ['services/a','services/b','contact'])+'</urlset>'
    f=FakeFetcher({base:html_response(base,'<h1>Home</h1>'),base+'sitemap.xml':SiteFetchResponse(requested_url=base+'sitemap.xml',final_url=base+'sitemap.xml',status_code=200,content_type='application/xml',body=sitemap.encode()),base+'contact':html_response(base+'contact','<h1>Contact Us</h1>')})
    result=await worker(f).collect(site_url=base,discovery_limit=2,deep_analysis_limit=2)
    assert result.discovered_url_count==2 and result.selected_pages[1].page_type=='contact'
    home='<script type="application/ld+json">{"@type":"ContactPage"}</script><h1>Contact Us</h1><a href="tel:123456789">Phone</a><p>Opening Hours Monday 09:00-17:00</p>'
    f=FakeFetcher({base:html_response(base,home)})
    result=await worker(f).collect(site_url=base,discovery_limit=2,deep_analysis_limit=2)
    assert not any('contact_not_checked:coverage' in x for x in result.limitations)
    assert result.deep_analyzed_count==1
