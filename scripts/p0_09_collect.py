"""Local, reviewer-confirmed gold collection; no production persistence or account."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4, uuid5, NAMESPACE_URL

from p0_09_candidates import ROOT, SITES, safe_text, dotenv_values, configured_serpapi_keys, Settings, fakeredis, JobCheckpoints, normalize_domain
from p0_09_adjudication import OUTPUT as ADJ, PRIOR
from app.api.v2.models import AnalyzeRequest, CompetitorCandidate
from app.api.v2.competitor_models import CompetitorDiscoveryResult
from app.collectors.serp_market_models import SerpMarketSnapshot
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.competitors_v22.selection import analysis_discovery_input_digest, validate_confirmed_competitors
from app.competitors_v22.collection_stage import CheckpointedCompetitorCollectionStage
from app.competitors_v22.site_stage import CheckpointedCompetitorSiteStage
from app.competitors_v22.public_profile_stage import build_public_profile_stage
from app.jobs_v22.site_inventory_stage import build_site_inventory_stage
from app.jobs_v22.site_render_stage import build_site_render_stage
from app.jobs_v22.customer_public_gbp_stage import build_customer_public_gbp_stage
from app.jobs_v22.capability_acquisition_input import build_acquisition_input, stamp
from app.jobs_v22.cost_models import OPERATION_LIMITS
from app.jobs_v22.cost_ledger import CostLimitExceeded
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.preflight_v22.gbp import _normalize_candidate
from app.preflight_v22.gbp import _market_from_result
from app.competitors_v22.normalization import canonical_competitor_url
from app.report_v22.capability_public import build_public_capability_report

OUTPUT=ROOT/'tests/fixtures/gold/p0-09-full-2026-11-03'


def utc():return datetime.now(timezone.utc)


class Allowance:
    def __init__(self, directory, prior, secrets):
        self.directory,self.events,self.secrets=directory,prior.copy(),secrets
        self.counts=Counter();self.lock=asyncio.Lock()
    def serp_count(self):return sum(not e['operation'].startswith('firecrawl') for e in self.events)
    def fire_count(self):return sum(e['operation'].startswith('firecrawl') for e in self.events)
    def reserve(self):return Decimal(self.serp_count())*Decimal('.025')+Decimal(self.fire_count())*Decimal('.005')
    def view(self,job):
        parent=self
        class View:
            async def claim(self,operation):
                async with parent.lock:
                    fire=operation.startswith('firecrawl')
                    if parent.counts[(job,operation)]>=OPERATION_LIMITS[operation] or (parent.fire_count()>=30 if fire else parent.serp_count()>=185) or parent.reserve()+(Decimal('.005') if fire else Decimal('.025'))>5:
                        raise CostLimitExceeded(operation)
                    event=dict(id=str(uuid4()),job_id=str(job),operation=operation,reserved_at=stamp(utc()))
                    with (parent.directory/f"attempt-{len(parent.events)+1:03}.json").open('x') as f:f.write(safe_text(event,parent.secrets))
                    parent.events.append(event);parent.counts[(job,operation)]+=1
                    return SimpleNamespace(claim_id=event['id'])
            async def complete(self,claim_id,*,outcome,duration_ms):
                with (parent.directory/f'completed-{claim_id}.json').open('x') as f:f.write(safe_text(dict(id=str(claim_id),outcome=outcome,duration_ms=duration_ms),parent.secrets))
            async def record_checkpoint_hit(self,**kwargs):pass
        return View()


def check_manifest(root, name):
    for p,h in json.loads((root/name).read_text()).items():
        if hashlib.sha256((root/p).read_bytes()).hexdigest()!=h:raise RuntimeError('saved_evidence_changed')


async def run(resume=None):
    global OUTPUT
    if resume is not None:OUTPUT=ROOT/'tests/fixtures/gold/p0-09-full-continuation-2026-11-03'
    check_manifest(PRIOR,'checksums.json');check_manifest(ADJ,'delivery-checksums.json')
    prior=[json.loads(p.read_text()) for root in [PRIOR,ADJ] for p in sorted((root/'attempts').glob('attempt-*.json'))]
    if len(prior)!=76:raise RuntimeError('cumulative_budget_mismatch')
    if resume is not None:
        prior += [json.loads(p.read_text()) for p in sorted((resume/'attempts').glob('attempt-*.json'))]
    # Four 3-competitor sites + one 2-competitor site, including key rotation.
    if len(prior)+4*(3+15)+(3+10)>185:raise RuntimeError('projected_serp_budget_exceeded')
    env=dotenv_values(ROOT/'.env');names=('SERPAPI_KEY','SERPAPI_KEY_SECONDARY','SERPAPI_KEY_TERTIARY','FIRECRAWL_API_KEY')
    values={n:env.get(n) or '' for n in names};secrets=[s for s in values.values() if s]
    if not values['SERPAPI_KEY'] or not values['FIRECRAWL_API_KEY']:raise RuntimeError('provider_not_configured')
    OUTPUT.mkdir(exist_ok=False);(OUTPUT/'attempts').mkdir()
    budget=Allowance(OUTPUT/'attempts',prior,secrets)
    settings=Settings(_env_file=None,**values,V22_SITE_RENDER_FALLBACK_ENABLED=True)
    redis=fakeredis.aioredis.FakeRedis()
    cp=JobCheckpoints(redis,prefix='p009-full-local',ttl_seconds=86400)
    if resume is not None:
        for row in json.loads((resume/'local-checkpoints.json').read_text()):await redis.set(row['key'],row['value'])
    site_stage=build_site_inventory_stage(settings)
    competitor_stage=CheckpointedCompetitorCollectionStage(site_stage=CheckpointedCompetitorSiteStage(site_stage),profile_stage=build_public_profile_stage(settings))
    render_stage=build_site_render_stage(settings)
    results=[]
    def save(path,value):
        value=json.loads(json.dumps(value,default=lambda x:x.model_dump(mode='json') if hasattr(x,'model_dump') else str(x)))
        with path.open('x') as f:f.write(safe_text(value,secrets))
    def summary():
        return dict(sites=results,serpapi_attempts=budget.serp_count(),firecrawl_attempts=budget.fire_count(),
                    firecrawl_credits_reserved=budget.fire_count(),usd_usage_reserve=str(budget.reserve()),actual_invoice_usd=None,
                    remaining_serpapi_attempts=185-budget.serp_count(),remaining_firecrawl_credits=30-budget.fire_count(),remaining_usd_reserve=str(5-budget.reserve()))
    save(OUTPUT/'preflight.json',dict(prior_attempts=len(prior),projected_serpapi_max=len(prior)+85,projected_firecrawl_credits_max=29,
        local_only=True,site_render_enabled=True,site_map_enabled=settings.V22_SITE_INVENTORY_FIRECRAWL_ENABLED))
    try:
        for specification in SITES:
            site=dict(specification);folder=OUTPUT/site['id'];folder.mkdir();started=utc();job=uuid4();case=uuid5(NAMESPACE_URL,'p009-gold:'+site['id']);user=uuid5(NAMESPACE_URL,'p009-local-test-no-account')
            if resume is not None:
                old_result=json.loads((resume/site['id']/'result.json').read_text())
                from uuid import UUID
                job=UUID(old_result['job_id']);started=datetime.fromisoformat(old_result['started_at'].replace('Z','+00:00'))
            ledger=budget.view(job);n=len(budget.events)
            result=dict(site_id=site['id'],job_id=str(job),case_id=str(case),started_at=stamp(started),status='collecting')
            try:
                previous=json.loads((ADJ/site['id']/'result.json').read_text())
                market=SerpMarketSnapshot.model_validate_json((ADJ/site['id']/'market-snapshot.json').read_text())
                if market.completed_at+timedelta(days=1)<=utc():raise RuntimeError('approved_market_expired')
                cid='16921898606511158364' if site['id']=='seamless-gutters-pensacola' else previous['gbp']['public_gbp_url'].split('cid=')[1]
                # Only the confirmed reference is required for the detail stage;
                # no placeholder operating model is constructed before reading it.
                reference_request=SimpleNamespace(case_id=case,business_identity=SimpleNamespace(site_url=site['url'],public_gbp_url='https://www.google.com/maps?cid='+cid))
                gbp_stage=build_customer_public_gbp_stage(settings);delegate=gbp_stage.provider.request;observed={}
                async def capture(params,**kwargs):
                    response=await delegate(params,**kwargs)
                    raw=response.payload.get('place_results',{})
                    candidate=_normalize_candidate(raw,reference_request.business_identity.public_gbp_url)
                    if candidate:observed.update(candidate=asdict(candidate),gps=raw.get('gps_coordinates'),service_area_business=raw.get('service_area_business'),pure_service_area_business=raw.get('pure_service_area_business'))
                    return response
                gbp_stage.provider.request=capture
                gbp=await gbp_stage.collect(job_id=job,request=reference_request,submitted_at=started,checkpoints=cp,cost_ledger=ledger)
                if not observed and resume is not None:observed.update(json.loads((resume/site['id']/'identity-observation.json').read_text()))
                save(folder/'customer-gbp.json',gbp);save(folder/'identity-observation.json',observed)
                candidate=observed['candidate']
                mode='service_area' if site['id']=='seamless-gutters-pensacola' else candidate.get('operating_model')
                if not mode:raise RuntimeError('operating_model_unresolved')
                # The declared market stays Swansea/Hilo; primary location is
                # the actual profile location, never substituted with that market.
                primary=candidate.get('market')
                if hasattr(primary,'model_dump'):primary=primary.model_dump(mode='json')
                if isinstance(primary,str):
                    primary=_market_from_result(dict(address=candidate.get('address'),gps_coordinates=observed.get('gps')))
                    if primary is not None:primary=primary.model_dump(mode='json')
                if not primary:
                    primary=dict(display_name=candidate.get('address') or site['city'],country_code=site['country'])
                    gps=observed.get('gps') or {}
                    if 'latitude' in gps and 'longitude' in gps:primary.update(latitude=gps['latitude'],longitude=gps['longitude'])
                competitors=[];candidates=[]
                for row in previous['selected_competitors']:
                    domain=normalize_domain(row['url']);key='cp_'+hashlib.sha256((row.get('provider_cid') or domain).encode()).hexdigest()[:24]
                    selected_records=[r for q in market.query_runs for r in q.results if r.normalized_domain and normalize_domain(str(r.url))==domain]
                    c=dict(competitor_id=key,business_name=row['display_name'],website_url=canonical_competitor_url(row['url']),public_gbp_url='https://www.google.com/maps?cid='+row['provider_cid'])
                    competitors.append(dict(c,confirmation_source='user'))
                    candidates.append(CompetitorCandidate(**c,query_appearance_count=len({r.query for r in selected_records}),best_position=min(r.position for r in selected_records),confidence='medium',relevance_reason='Reviewer-confirmed Q1 maps selection; saved city-point market records.'))
                request=AnalyzeRequest.model_validate_json(json.dumps(dict(case_id=str(case),report_type='prospect',
                    business_identity=dict(business_name=candidate['business_name'],site_url=site['url'],normalized_domain=normalize_domain(site['url']),operating_model=mode,primary_location=primary,public_gbp_url=reference_request.business_identity.public_gbp_url),
                    primary_service=site['service'],target_market=dict(display_name=market.target_point.requested_label,city=site['city'],region=site['region'],country_code=site['country'],latitude=market.target_point.latitude,longitude=market.target_point.longitude),
                    queries=site['queries'],competitors=competitors,generation_limits=dict(competitor_count=len(competitors)))))
                digest=analysis_discovery_input_digest(request)
                shared=await SharedMarketSnapshotStore(redis,prefix='p009-local',ttl_seconds=86400).save(input_digest=digest,snapshot=market,now=market.completed_at)
                discovery=CompetitorDiscoveryResult(discovery_id=uuid4(),case_id=case,input_digest=digest,candidate_digest=request_digest(dict(schema_version='gold_reviewer_confirmed_candidates_v1',source_market_checksum=shared.snapshot_checksum,candidates=[c.model_dump(mode='json') for c in candidates])),market_snapshot_id=shared.snapshot_id,market_snapshot_checksum=shared.snapshot_checksum,candidates=candidates,ready_for_confirmation=True,data_gaps=[],limitations=['Local reviewer-confirmed candidate set; no production discovery task.'],created_at=started,expires_at=shared.expires_at)
                validate_confirmed_competitors(request=request,candidates=discovery.candidates)
                save(folder/'confirmed-request.json',request);save(folder/'discovery.json',discovery);save(folder/'shared-market.json',shared)
                scope=dict(id=str(uuid4()),user_id=str(user),case_id=str(case),revision=1,site_url=site['url'],primary_service=site['service'],target_market=request.target_market.display_name,target_pages=[site.get('target',site['url'])],competitor_sites=[str(c.website_url) for c in request.competitors],priority=None,confirmation_source='seo_confirmation',confirmed_at=stamp(started))
                print(json.dumps(dict(site=site['id'],stage='collecting_site')),flush=True)
                inventory=await site_stage.collect(job_id=job,request=request,checkpoints=cp,cost_ledger=ledger)
                save(folder/'site-inventory.json',inventory)
                print(json.dumps(dict(site=site['id'],stage='collecting_competitors')),flush=True)
                rivals=await competitor_stage.collect(job_id=job,request=request,discovery=discovery,shared_market=shared,checkpoints=cp,cost_ledger=ledger)
                save(folder/'competitors.json',rivals)
                print(json.dumps(dict(site=site['id'],stage='render_gate')),flush=True)
                rendered=await render_stage.collect(job_id=job,business=SimpleNamespace(**scope),static=inventory,gbp=gbp.snapshot,country=site['country'],checkpoints=cp,cost_ledger=ledger)
                save(folder/'rendered.json',rendered if rendered is not None else dict(snapshot=None,reason='no_page_required_render'))
                collected=SimpleNamespace(site_inventory=inventory,shared_market=shared,competitor_collection=rivals,customer_public_gbp=gbp,discovery=discovery)
                manifest,value=build_acquisition_input(user_id=user,job_id=job,business=scope,request=request,collected=collected,created_at=started,deadline_at=started+timedelta(hours=3),now=utc(),rendered=rendered)
                # Validate before installing the new descriptor. Frozen source
                # files above remain available even when binding reveals a bug.
                report=build_public_capability_report(manifest,value)
                save(folder/'bound.json',dict(manifest=manifest.model_dump(mode='json'),request=value.model_dump(mode='json')))
                save(folder/'report.json',report)
                result.update(status='bound_input_ready',report_findings=len(report.findings),render_snapshot=rendered is not None,render_pages=len(rendered.pages) if rendered else 0)
            except Exception as exc:
                result.update(status='blocked',error_type=type(exc).__name__,error_code=getattr(exc,'error_code',None))
                # Only internal static error messages, never provider payloads.
                if type(exc) is RuntimeError:result['reason']=str(exc) if str(exc) in {'approved_market_expired','operating_model_unresolved'} else 'local_collection_error'
            result.update(completed_at=stamp(utc()),attempts_added=len(budget.events)-n)
            save(folder/'result.json',result);results.append(result)
            print(json.dumps(result),flush=True)
        save(OUTPUT/'summary.json',summary())
        print(safe_text(summary(),secrets),flush=True)
    finally:
        # Freeze a key-free Redis checkpoint export for recovery; no remote
        # Redis connection exists. Scrub configured secrets from all exports.
        rows=[]
        async for key in redis.scan_iter('*'):
            if await redis.type(key)==b'string':rows.append(dict(key=key.decode(),value=(await redis.get(key)).decode()))
        save(OUTPUT/'local-checkpoints.json',rows)
        save(OUTPUT/'final-budget.json',summary())
        await redis.aclose()


def self_test():
    import tempfile
    async def test():
        with tempfile.TemporaryDirectory() as directory:
            prior=[dict(operation='serpapi_prior') for _ in range(76)]
            allowance=Allowance(Path(directory),prior,[]);view=allowance.view(uuid4())
            for _ in range(3):await view.claim('serpapi_customer_public_gbp')
            try:await view.claim('serpapi_customer_public_gbp')
            except CostLimitExceeded:pass
            else:raise AssertionError('Per-site attempt limit bypassed')
            for _ in range(2):await view.claim('firecrawl_scrape')
            assert allowance.serp_count()==79 and allowance.fire_count()==2
            assert allowance.reserve()==Decimal('1.985')
    asyncio.run(test());print('PASS: cumulative provider budgets, per-site operation caps, mixed-provider pricing')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--self-test',action='store_true');parser.add_argument('--resume',type=Path);args=parser.parse_args()
    try:
        if args.self_test:self_test()
        else:asyncio.run(run(args.resume))
    except Exception as exc:
        print(json.dumps(dict(status='STOPPED',error_type=type(exc).__name__)),flush=True)
        raise SystemExit(1) from None
