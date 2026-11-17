"""Offline checks for the new collector namespace and bounded response reuse."""
import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from app.collectors.site_inventory import SiteInventoryCollector
from app.collectors.site_inventory_fetcher import SiteFetchResponse
from app.collectors.site_inventory_html import extract_html_structure
from app.collectors.site_inventory_reuse import restored_record
from app.collectors.site_inventory_urls import SiteScope
from app.jobs_v22.site_inventory_stage import CheckpointedSiteFetcher, DEEP_STRUCTURE_CHECKPOINT_VERSION
from app.report_v22.evidence_adapters.site_counts import eligible_html
from test_v22_site_inventory_models import page
from test_v22_site_inventory_collector import NOW
from test_v22_site_inventory_stage import JOB_ID

@pytest.fixture
def anyio_backend(): return 'asyncio'

URL='https://example.com/services/repair'
SCOPE=SiteScope.from_root('https://example.com/')
HTML=b'<title>Repair service</title><h1>Repairs</h1><link rel="canonical" href="/services/repair"><meta name="robots" content="noindex"><script type="application/ld+json">{"@type":"Service"}</script><a href="/contact">Contact</a>'

def original():
 from app.collectors.site_inventory_models import InventoryPageRecordV2
 return InventoryPageRecordV2.model_validate({**page(URL).model_dump(mode='json'),'structural_truncated':True,'title':None,'h1':None,'canonical_url':None,'schema_types':[],'meta_robots':[],'page_type':'other'})

def response(): return SiteFetchResponse(URL,URL,200,'text/html',HTML)

def structure(): return extract_html_structure(HTML.decode(),page_url=URL,scope=SCOPE)

def test_successful_full_deep_response_restores_metadata_and_preserves_record_type_and_inputs():
 old=original();before=old.model_dump(mode='json');r=response()
 new=restored_record(old,r,structure(),scope=SCOPE,max_bytes=2_000_000)
 assert type(new) is type(old) and not new.structural_truncated and eligible_html(new)
 assert new.title=='Repair service' and new.h1=='Repairs'
 assert new.meta_robots==['noindex'] and new.schema_types==['Service']
 assert str(new.canonical_url)==URL and [str(u) for u in new.internal_links]==['https://example.com/contact']
 assert new.page_type=='service_detail' and new.response_bytes==len(HTML)
 assert old.model_dump(mode='json')==before and r.body==HTML

@pytest.mark.parametrize('mutation',['different_url','off_host','unsupported_media','truncated','failure','oversize'])
def test_invalid_or_unbounded_deep_response_never_upgrades_a_prefix(mutation):
 old=original();r=response();budget=2_000_000
 if mutation=='different_url':r=replace(r,requested_url='https://example.com/another')
 if mutation=='off_host':r=replace(r,final_url='https://foreign.example/')
 if mutation=='unsupported_media':r=replace(r,content_type='application/pdf')
 if mutation=='truncated':r=replace(r,truncated=True)
 if mutation=='failure':r=replace(r,status_code=404)
 if mutation=='oversize':budget=len(HTML)-1
 assert restored_record(old,r,structure(),scope=SCOPE,max_bytes=budget) is old

class MemoryCheckpoints:
 def __init__(self):self.values={};self.writes=[]
 async def get(self,job,key):return deepcopy(self.values.get(key))
 async def run_once(self,job,key,operation):
  if key not in self.values:self.values[key]=await operation();self.writes.append(key)
  return deepcopy(self.values[key])

@pytest.mark.anyio
async def test_new_namespace_validates_and_reuses_legacy_raw_bytes_without_overwriting_or_provider_calls():
 delegate=AsyncMock();delegate.fetch.side_effect=AssertionError('NO_NETWORK')
 checkpoints=MemoryCheckpoints();legacy=CheckpointedSiteFetcher(delegate=delegate,checkpoints=checkpoints,job_id=JOB_ID)
 key=legacy.checkpoint_key(URL,max_bytes=2_000_000,accepted_media_types={'text/html'})
 saved={'schema_version':'site_inventory_fetch_v1','requested_url':URL,'final_url':URL,'status_code':200,'content_type':'text/html','body_base64':base64.b64encode(HTML).decode(),'body_checksum':'sha256:'+hashlib.sha256(HTML).hexdigest()}
 checkpoints.values[key]=saved;before=deepcopy(saved)
 current=CheckpointedSiteFetcher(delegate=delegate,checkpoints=checkpoints,job_id=JOB_ID,checkpoint_namespace=DEEP_STRUCTURE_CHECKPOINT_VERSION)
 for _ in range(2):assert (await current.fetch(URL,scope=SCOPE,max_bytes=2_000_000,accepted_media_types={'text/html'})).body==HTML
 assert checkpoints.values[key]==before and checkpoints.writes==[DEEP_STRUCTURE_CHECKPOINT_VERSION+':'+key]
 delegate.fetch.assert_not_called()

@pytest.mark.anyio
async def test_legacy_raw_corruption_is_not_accepted_by_the_new_namespace():
 from app.jobs_v22.site_inventory_stage import SiteInventoryCheckpointError
 delegate=AsyncMock();checkpoints=MemoryCheckpoints();fetcher=CheckpointedSiteFetcher(delegate=delegate,checkpoints=checkpoints,job_id=JOB_ID,checkpoint_namespace=DEEP_STRUCTURE_CHECKPOINT_VERSION)
 key=fetcher.checkpoint_key(URL,max_bytes=2_000_000,accepted_media_types={'text/html'})
 checkpoints.values[key]={'schema_version':'site_inventory_fetch_v1','requested_url':URL,'final_url':URL,'status_code':200,'content_type':'text/html','body_base64':base64.b64encode(HTML).decode(),'body_checksum':'sha256:'+'0'*64}
 with pytest.raises(SiteInventoryCheckpointError):await fetcher.fetch(URL,scope=SCOPE,max_bytes=2_000_000,accepted_media_types={'text/html'})
 delegate.fetch.assert_not_called()

class PrefixFetcher:
 def __init__(self):self.calls=[]
 async def fetch(self,url,**options):
  self.calls.append((url,options))
  if url.endswith(('robots.txt','sitemap.xml')):return SiteFetchResponse(url,url,404,'text/plain',b'missing')
  prefix=b'<a href="/services/repair">Repair</a><a href="/contact">Contact</a>'
  tail=HTML if '/services/' in url else b'<title>Contact us</title><h1>Contact us</h1><a href="tel:123456789">Call</a>' if url.endswith('/contact') else b'<title>Example home</title><h1>Home</h1>'
  body=prefix+b' '*256001+tail
  if options.get('allow_prefix'):return SiteFetchResponse(url,url,200,'text/html',body[:options['max_bytes']],True)
  assert len(body)<=options['max_bytes'];return SiteFetchResponse(url,url,200,'text/html',body)
 def apply_crawl_delay(self,seconds):pass

async def collect(reuse,contact):
 f=PrefixFetcher();c=SiteInventoryCollector(fetcher=f,firecrawl=None,structural_max_bytes=256000,sitemap_max_bytes=100000,sitemap_decompressed_max_bytes=500000,sitemap_max_files=5,sitemap_max_depth=2,batch_size=1,resource_limits=True,sampling_quality=True,contact_coverage=contact,reuse_deep_structure=reuse,clock=lambda:NOW)
 result=await c.collect(site_url='https://example.com/',discovery_limit=50,deep_analysis_limit=10)
 return f,result

@pytest.mark.anyio
@pytest.mark.parametrize('contact',[False,True])
async def test_customer_and_competitor_collectors_restore_prefixes_without_more_fetches_or_reselection(contact):
 old_fetch,old=await collect(False,contact);new_fetch,new=await collect(True,contact)
 assert new_fetch.calls==old_fetch.calls
 assert [str(p.url) for p in new.selected_pages]==[str(p.url) for p in old.selected_pages]
 assert all(eligible_html(p) for p in new.pages)
 assert any(p.structural_truncated for p in old.pages)
 assert 'service_detail' in [p.page_type for p in new.pages]
 assert all(p.page_type==p.deep_snapshot.page_type for p in new.selected_pages)
 assert [p.deep_snapshot.content_checksum for p in new.selected_pages]==[p.deep_snapshot.content_checksum for p in old.selected_pages]
 assert new.discovery_limit==50 and new.deep_analysis_limit==10
 assert not any('bounded prefix' in note for note in new.limitations)


@pytest.mark.anyio
async def test_current_snapshot_namespace_never_returns_the_legacy_snapshot_and_keeps_old_keys_unchanged():
 import fakeredis.aioredis
 from app.jobs_v22.checkpoints import JobCheckpoints
 from test_v22_site_inventory_stage import stage,RecordingFetcher,InterruptingFirecrawl,prospect_request
 redis=fakeredis.aioredis.FakeRedis();checkpoints=JobCheckpoints(redis,prefix='reuse-test',ttl_seconds=600)
 fetcher=RecordingFetcher();mapper=InterruptingFirecrawl();mapper.calls=1
 legacy=stage(fetcher,mapper);legacy.resource_limits=True;legacy.sampling_quality=True;legacy.contact_coverage=True
 old=await legacy.collect(job_id=JOB_ID,request=prospect_request(),checkpoints=checkpoints)
 saved={k:await redis.get(k) for k in await redis.keys('*')};calls=list(fetcher.calls);map_calls=mapper.calls
 current=stage(fetcher,mapper);current.resource_limits=True;current.sampling_quality=True;current.contact_coverage=True;current.reuse_deep_structure=True
 first=await current.collect(job_id=JOB_ID,request=prospect_request(),checkpoints=checkpoints)
 again=await current.collect(job_id=JOB_ID,request=prospect_request(),checkpoints=checkpoints)
 assert first==again and first.schema_version==old.schema_version=='site_inventory_snapshot_v3'
 assert fetcher.calls==calls and mapper.calls==map_calls
 assert all([await redis.get(k)==v for k,v in saved.items()])
 assert len(await redis.keys('*'))>len(saved)
 await redis.aclose()

@pytest.mark.anyio
async def test_pages_without_a_successful_deep_response_remain_prefixes():
 f=PrefixFetcher();c=SiteInventoryCollector(fetcher=f,firecrawl=None,structural_max_bytes=256000,sitemap_max_bytes=100000,sitemap_decompressed_max_bytes=500000,sitemap_max_files=5,sitemap_max_depth=2,batch_size=1,resource_limits=True,reuse_deep_structure=True,clock=lambda:NOW)
 result=await c.collect(site_url='https://example.com/',discovery_limit=50,deep_analysis_limit=1)
 assert sum(eligible_html(p) for p in result.pages)==1
 assert all(p.structural_truncated for p in result.pages if str(p.url)!='https://example.com/')
 assert any('bounded prefix' in note for note in result.limitations)


def test_production_factory_selects_new_collection_namespace_only_for_resource_limited_collection():
 from app.core.config import Settings
 from app.jobs_v22.site_inventory_stage import build_site_inventory_stage
 settings=Settings(_env_file=None)
 assert build_site_inventory_stage(settings,resource_limits=True).reuse_deep_structure is True
 assert build_site_inventory_stage(settings).reuse_deep_structure is False
 assert settings.V22_SITE_INVENTORY_STRUCTURAL_BYTES==256000
 assert settings.V22_CAPABILITY_FREEZE_MAX_INPUT_BYTES==40000000
