#!/usr/bin/env python3
"""P0-04 diagnostic: fresh public HTML plus the enabled acquisition render gate.

Run from the backend root with its test dependencies installed. GBP values are
explicit comparison fixtures, not a live profile lookup. Provider calls, if
needed after a future page change, use the normal two-attempt cost ledger.
"""
import asyncio,json,hashlib,sys
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from uuid import UUID
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT), str(ROOT/'tests')]
import fakeredis.aioredis
from app.collectors.site_inventory_fetcher import BoundedSiteFetcher
from app.collectors.site_inventory_urls import SiteScope
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.jobs_v22.site_render_stage import build_site_render_stage,static_contact_check
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import PricingCatalog
from app.jobs_v22.digest import canonical_json_bytes
from app.core.config import Settings
from site_business_helpers import request
from test_p0_03_text_patterns import run,field
from public_gbp_helpers import public_source
async def main():
 url='https://visiontechav.com/swansea/'
 fetcher=BoundedSiteFetcher(connect_timeout=10,read_timeout=20,total_timeout=40,max_redirects=4,concurrency=1,requests_per_second=1)
 response=await fetcher.fetch(url,scope=SiteScope.from_root(url),max_bytes=2000000,accepted_media_types={'text/html','application/xhtml+xml'})
 html=response.decode()
 sample=request(html.strip()).source.payload.model_dump(mode='json')
 def rewrite(x):
  if isinstance(x,dict):return {k:rewrite(v) for k,v in x.items()}
  if isinstance(x,list):return [rewrite(v) for v in x]
  if isinstance(x,str):return x.replace('https://example.test/',url).replace('example.test','visiontechav.com') if len(x)<2000 else x
  return x
 static=SiteInventorySnapshot.model_validate_json(json.dumps(rewrite(sample)))
 now=datetime.now(timezone.utc)
 redis=fakeredis.aioredis.FakeRedis()
 settings=Settings();settings.V22_SITE_RENDER_FALLBACK_ENABLED=True
 stage=build_site_render_stage(settings)
 cp=JobCheckpoints(redis,prefix='p004-real',ttl_seconds=600)
 ledger=JobCostLedger(redis,prefix='p004-real',job_id=UUID(int=404),ttl_seconds=600,job_created_at=now-timedelta(minutes=1),pricing=PricingCatalog())
 before=canonical_json_bytes(static)
 check=static_contact_check(static.selected_pages[0].deep_snapshot,gbp_phone='0333 335 5510',gbp_address='Unit 9, Parc Gelli Werdd, Cross Hands, Llanelli SA14 6EF',country='GB')
 rendered=await stage.collect(job_id=UUID(int=404),business=SimpleNamespace(site_url=url,target_pages=[]),static=static,gbp=public_source().payload,country='GB',checkpoints=cp,cost_ledger=ledger)
 facts,_,alignment,_=run(html,phone='0333 335 5510',address='Unit 9, Parc Gelli Werdd, Cross Hands, Llanelli SA14 6EF',country='GB')
 costs=(await ledger.snapshot()).root
 print(json.dumps({'url':url,'fetched_at':now.isoformat(),'status':response.status_code,'bytes':len(response.body),'sha256':hashlib.sha256(response.body).hexdigest(),
 'flag_enabled':True,'static_contact_observed':check[0],'visible_characters':check[1],
 'rendered_snapshot_created':rendered is not None,'firecrawl_attempts':costs['firecrawl_attempts'],
 'static_snapshot_unchanged':before==canonical_json_bytes(static),'phone_candidates':sorted({c.scalar_value for c in facts.candidates if c.field=='phone'}),
 'before_phone':{'state':field(alignment,'phone').state,'reason':field(alignment,'phone').not_checked_reason},
 'after_phone':({'state':field(alignment,'phone').state,'reason':field(alignment,'phone').not_checked_reason} if rendered is None else {'state':'not_evaluated_by_this_probe','reason':'page_changed_use_offline_report_tests'}),
 'address':field(alignment,'address').state,'hours':sorted({c.scalar_value for c in facts.candidates if c.field=='opening_hours'}),
 'gbp_reference':'synthetic comparison reference, not a newly collected Google profile'},ensure_ascii=False,indent=2))
 await redis.aclose()
asyncio.run(main())
