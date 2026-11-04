"""Offline freeze of actual P0-09 collected graphs; never edits source evidence."""
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5, NAMESPACE_URL
from datetime import datetime,timedelta,timezone
from p0_09_collect import ROOT,SITES,stamp,build_acquisition_input
from p0_09_collect import fakeredis,SharedMarketSnapshotStore,analysis_discovery_input_digest,validate_confirmed_competitors,request_digest
from p0_09_candidates import safe_text,dotenv_values
from app.api.v2.models import AnalyzeRequest
from app.api.v2.competitor_models import CompetitorDiscoveryResult
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.collectors.site_render_models import RenderedSiteSnapshot
from app.competitors_v22.models import CompetitorCollectionSnapshot,SharedMarketSnapshot
from app.jobs_v22.customer_public_gbp_stage import CustomerPublicGbpCollection
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.capability_public import build_public_capability_report

SOURCE=ROOT/'tests/fixtures/gold/p0-09-full-continuation-2026-11-03'
OUTPUT=ROOT/'tests/fixtures/gold/p0-09-frozen-v2-2026-11-03'


async def main():
    OUTPUT.mkdir(exist_ok=False)
    env=dotenv_values(ROOT/'.env');secrets=[v for k,v in env.items() if v and k in ('SERPAPI_KEY','SERPAPI_KEY_SECONDARY','SERPAPI_KEY_TERTIARY','FIRECRAWL_API_KEY')]
    def save(path,value):
        with path.open('x') as f:f.write(safe_text(value,secrets))
    rows=[]
    for site in SITES:
        folder=SOURCE/site['id'];out=OUTPUT/site['id'];out.mkdir()
        result=dict(site_id=site['id'])
        try:
            def model(name,cls):return cls.model_validate_json((folder/name).read_text())
            original_request=model('confirmed-request.json',AnalyzeRequest)
            corrected=original_request.model_dump(mode='json')
            corrected['business_identity']['primary_location']=corrected['target_market']
            request=AnalyzeRequest.model_validate_json(json.dumps(corrected))
            state=json.loads((folder/'result.json').read_text())
            started=datetime.fromisoformat(state['started_at'].replace('Z','+00:00'));job=UUID(state['job_id']);user=uuid5(NAMESPACE_URL,'p009-local-test-no-account')
            scope=dict(id=str(uuid5(NAMESPACE_URL,'p009-gold-scope:'+site['id'])),user_id=str(user),case_id=str(request.case_id),revision=1,
                site_url=site['url'],primary_service=site['service'],target_market=request.target_market.display_name,
                target_pages=[site.get('target',site['url'])],competitor_sites=[str(c.website_url) for c in request.competitors],priority=None,
                confirmation_source='seo_confirmation',confirmed_at=stamp(started))
            render=json.loads((folder/'rendered.json').read_text())
            rendered=RenderedSiteSnapshot.model_validate_json(json.dumps(render)) if render.get('schema_version') else None
            old_shared=model('shared-market.json',SharedMarketSnapshot);old_discovery=model('discovery.json',CompetitorDiscoveryResult)
            original_rivals=model('competitors.json',CompetitorCollectionSnapshot)
            # Create NEW local confirmation/binding envelopes for the approved
            # request semantics. Source observation bodies and old files stay
            # intact; no old snapshot timestamp or expiry is refreshed.
            redis=fakeredis.aioredis.FakeRedis()
            shared=await SharedMarketSnapshotStore(redis,prefix='p009-freeze',ttl_seconds=86400).save(input_digest=analysis_discovery_input_digest(request),snapshot=old_shared.snapshot,now=old_shared.created_at)
            await redis.aclose()
            discovery=old_discovery.model_copy(update=dict(discovery_id=uuid5(NAMESPACE_URL,'p009-confirmed-market-context:'+site['id']),input_digest=shared.input_digest,
                candidate_digest=request_digest(dict(schema_version='gold_reviewer_confirmed_candidates_v1',input_digest=shared.input_digest,source_market_checksum=shared.snapshot_checksum,candidates=[c.model_dump(mode='json') for c in old_discovery.candidates])),market_snapshot_id=shared.snapshot_id))
            discovery=CompetitorDiscoveryResult.model_validate_json(canonical_json_bytes(discovery))
            validate_confirmed_competitors(request=request,candidates=discovery.candidates)
            rivals=original_rivals.model_copy(update=dict(discovery_id=discovery.discovery_id,candidate_digest=discovery.candidate_digest,market_snapshot_id=shared.snapshot_id))
            rivals=CompetitorCollectionSnapshot.model_validate_json(canonical_json_bytes(rivals))
            assert canonical_json_bytes(shared.snapshot)==canonical_json_bytes(old_shared.snapshot)
            assert canonical_json_bytes([c.model_dump(mode='json') for c in rivals.competitors])==canonical_json_bytes([c.model_dump(mode='json') for c in original_rivals.competitors])
            save(out/'binding-provenance.json',dict(reason='User confirmed primary_location is sampling context; no GBP address change',
                original_request_checksum=request_digest(original_request),confirmed_request_checksum=request_digest(request),
                original_shared_market_id=str(old_shared.snapshot_id),new_shared_market_id=str(shared.snapshot_id),
                market_observations_unchanged=True,competitor_observations_unchanged=True,
                source_directory=str(folder.relative_to(ROOT)),local_technical_user_id=True))
            save(out/'confirmed-request.json',request.model_dump(mode='json'));save(out/'confirmed-discovery.json',discovery.model_dump(mode='json'))
            collected=SimpleNamespace(site_inventory=model('site-inventory.json',SiteInventorySnapshot),shared_market=shared,
                competitor_collection=rivals,customer_public_gbp=model('customer-gbp.json',CustomerPublicGbpCollection),discovery=discovery)
            manifest,value=build_acquisition_input(user_id=user,job_id=job,business=scope,request=request,collected=collected,created_at=started,deadline_at=started+timedelta(hours=3),now=datetime.now(timezone.utc),rendered=rendered)
            bound=dict(manifest=manifest.model_dump(mode='json'),request=value.model_dump(mode='json'))
            # Freeze the validated source graph even if the finding generator
            # rejects its business-vs-target location semantics.
            save(out/'bound.json',bound)
            result['frozen']=True
            try:
                report=build_public_capability_report(manifest,value)
                save(out/'report.json',report.model_dump(mode='json'));result['generation']='passed'
            except Exception as exc:
                result.update(generation='blocked',error_type=type(exc).__name__,error_code=getattr(exc,'error_code',None))
            # input.json is a dataset descriptor, not a frozen observation.
            # Keep the prior descriptor as a new provenance file before update.
            case=ROOT/'tests/fixtures/gold'/site['id'];descriptor=json.loads((case/'input.json').read_text())
            save(out/'previous-input-descriptor.json',descriptor)
            target=case/'bound-p0-09-2026-11-03.json'
            with target.open('xb') as f:f.write((out/'bound.json').read_bytes())
            reference=dict(base='case',path=target.name,sha256=hashlib.sha256(target.read_bytes()).hexdigest())
            descriptor['generation']=dict(kind='bound_input',input=reference)
            descriptor['saved_page_url']=descriptor['site_url']
            descriptor['site_url']=site['url']
            descriptor['target_pages']=[site.get('target',site['url'])]
            descriptor.pop('pending_input',None);descriptor['snapshot_files'].append(reference)
            (case/'input.json').write_text(json.dumps(descriptor,ensure_ascii=False,indent=2)+'\n')
            result['descriptor_installed']=True
        except Exception as exc:
            result.update(frozen=False,error_type=type(exc).__name__,error_code=getattr(exc,'error_code',None))
        save(out/'result.json',result);rows.append(result)
    save(OUTPUT/'summary.json',rows)
    save(OUTPUT/'checksums.json',{str(p.relative_to(OUTPUT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in OUTPUT.rglob('*') if p.is_file()})
    print(json.dumps(rows,ensure_ascii=False,indent=2))


if __name__=='__main__':
    import asyncio
    try:asyncio.run(main())
    except Exception as exc:
        print(json.dumps(dict(status='STOPPED',error_type=type(exc).__name__)))
        raise SystemExit(1) from None
