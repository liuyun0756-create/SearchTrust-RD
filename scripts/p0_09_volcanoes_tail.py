"""Finish independent approved competitor work after Volcanoes root failure."""
import asyncio,json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from p0_09_collect import *
from app.collectors.site_inventory_urls import SiteScope
from app.collectors.site_inventory_fetcher import BoundedSiteFetcher

async def main():
    output=ROOT/'tests/fixtures/gold/p0-09-volcanoes-partial-2026-11-03';output.mkdir(exist_ok=False);(output/'attempts').mkdir()
    roots=[PRIOR,ADJ,ROOT/'tests/fixtures/gold/p0-09-full-2026-11-03',ROOT/'tests/fixtures/gold/p0-09-full-continuation-2026-11-03']
    events=[json.loads(p.read_text()) for r in roots for p in sorted((r/'attempts').glob('attempt-*.json'))]
    env=dotenv_values(ROOT/'.env');names=('SERPAPI_KEY','SERPAPI_KEY_SECONDARY','SERPAPI_KEY_TERTIARY','FIRECRAWL_API_KEY');values={n:env.get(n) or '' for n in names};secrets=[v for v in values.values() if v]
    allowance=Allowance(output/'attempts',events,secrets)
    if allowance.serp_count()+10>185:raise RuntimeError('projected_budget_exceeded')
    settings=Settings(_env_file=None,**values,V22_SITE_RENDER_FALLBACK_ENABLED=True)
    redis=fakeredis.aioredis.FakeRedis();cp=JobCheckpoints(redis,prefix='p009-volcanoes-tail',ttl_seconds=86400)
    folder=roots[-1]/'volcanoes-hiking-guides'
    request=AnalyzeRequest.model_validate_json((folder/'confirmed-request.json').read_text())
    discovery=CompetitorDiscoveryResult.model_validate_json((folder/'discovery.json').read_text())
    from app.competitors_v22.models import SharedMarketSnapshot
    shared=SharedMarketSnapshot.model_validate_json((folder/'shared-market.json').read_text())
    def save(name,value):
        if hasattr(value,'model_dump'):value=value.model_dump(mode='json')
        with (output/name).open('x') as f:f.write(safe_text(value,secrets))
    failures=[]
    profile=build_public_profile_stage(settings);original=profile.provider.request
    async def observed_request(params,**kwargs):
        try:return await original(params,**kwargs)
        except Exception as exc:
            failures.append(dict(engine=params.get('engine'),error_type=type(exc).__name__,http_status=getattr(exc,'status_code',None)))
            raise
    profile.provider.request=observed_request
    try:
        stage=CheckpointedCompetitorCollectionStage(site_stage=CheckpointedCompetitorSiteStage(build_site_inventory_stage(settings)),profile_stage=profile)
        result=await stage.collect(job_id=uuid4(),request=request,discovery=discovery,shared_market=shared,checkpoints=cp,cost_ledger=allowance.view(uuid4()))
        save('competitors.json',result);save('profile-provider-failures.json',failures)
        fetcher=BoundedSiteFetcher(connect_timeout=5,read_timeout=15,total_timeout=30,max_redirects=5,concurrency=1,requests_per_second=1)
        try:
            response=await fetcher.fetch(str(request.business_identity.site_url),scope=SiteScope.from_root(str(request.business_identity.site_url)),max_bytes=settings.V22_SITE_INVENTORY_STRUCTURAL_BYTES,accepted_media_types={'text/html','application/xhtml+xml'})
            save('root-probe.json',dict(http_status=response.status_code,bytes=len(response.body),structural_byte_limit=settings.V22_SITE_INVENTORY_STRUCTURAL_BYTES))
        except Exception as exc:save('root-probe.json',dict(error_type=type(exc).__name__,error_code=getattr(exc,'code',None),structural_byte_limit=settings.V22_SITE_INVENTORY_STRUCTURAL_BYTES))
        save('summary.json',dict(serpapi_attempts=allowance.serp_count(),firecrawl_attempts=allowance.fire_count(),firecrawl_credits_reserved=allowance.fire_count(),usd_usage_reserve=str(allowance.reserve()),actual_invoice_usd=None,remaining_serpapi_attempts=185-allowance.serp_count(),remaining_firecrawl_credits=30-allowance.fire_count(),remaining_usd_reserve=str(5-allowance.reserve()),render_status='not_run_no_static_inventory_not_forced'))
        print((output/'summary.json').read_text())
    finally:await redis.aclose()

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as exc:
        print(json.dumps(dict(status='STOPPED',error_type=type(exc).__name__)))
        raise SystemExit(1) from None
