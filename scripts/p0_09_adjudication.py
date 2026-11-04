"""Apply the P0-09 reviewer ruling locally; stop after candidate selection."""
import argparse
import asyncio
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from p0_09_candidates import (ROOT, SITES, Budget, safe_text, now, extract_site_signals,
    configured_serpapi_keys, execute_serpapi_get, dotenv_values, Settings,
    fakeredis, httpx, JobCheckpoints, build_serp_market_stage, SerpMarketContext,
    normalize_domain, _BLOCKED_PLATFORM_DOMAINS)
from app.collectors.serp_market_location import SerpApiLocationProvider
from app.preflight_v22.gbp import LimitedGbpLookup
from app.report_v22.site_gbp_comparators import compare_phones

PRIOR = ROOT / 'tests/fixtures/gold/p0-09-candidates-2026-11-03'
OUTPUT = ROOT / 'tests/fixtures/gold/p0-09-adjudication-2026-11-03'
REFERENCES = {'Fresno': (36.7378, -119.7871), 'Hilo': (19.7241, -155.0868),
              'Swansea': (51.62144, -3.943646), 'Pensacola': (30.44699, -87.237655)}
QUERIES = {'vision-tech-swansea': ['Vision-Tech AV Swansea', 'Vision Tech AV Ltd Swansea'],
           'seamless-gutters-pensacola': ['Seamless Gutters Pensacola', 'Seamless Gutters Pensacola FL']}
ACCEPTED = {'pr-plumbing-fresno':'9308775580876614598', 'all-star-plumbing-fresno':'3959786852779329755',
            'volcanoes-hiking-guides':'16906567364961418758'}


def distance(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [*a, *b])
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def matching(candidate, site, phones):
    domain = normalize_domain(candidate.get('website_url'))
    domain_match = bool(domain and domain == normalize_domain(site['url']))
    phone_match = bool(candidate.get('phone') and any(compare_phones(candidate['phone'], p, site['country']).state in
                       {'exact_match','semantic_match'} for p in phones))
    return dict(domain_match=domain_match, phone_match=phone_match, accepted=domain_match or phone_match)


def select_maps(snapshot, site, own_cid=None):
    rows, selected, seen = [], [], set()
    for record in snapshot.query_runs[0].results:
        if record.result_type != 'maps': continue
        row = record.model_dump(mode='json')
        domain = normalize_domain(str(record.url)) if record.url else None
        reason = None
        if domain == normalize_domain(site['url']) or (own_cid and record.provider_cid == own_cid): reason='client_business'
        elif not domain: reason='no_website'
        elif any(domain == d or domain.endswith('.'+d) for d in _BLOCKED_PLATFORM_DOMAINS): reason='platform_or_directory'
        elif domain == 'nps.gov' or domain.endswith('.nps.gov'): reason='national_park'
        elif not re.search(r'(?:^|,)\s*'+re.escape(site['city'])+r'(?:\s*,|\s+(?:[A-Z]{2}\s+\d|[A-Z]{1,2}\d)|\s*$)', record.address or '', re.I): reason='target_city_not_confirmed_in_address'
        elif domain in seen: reason='duplicate_website'
        if reason is None:
            seen.add(domain)
            if len(selected) < 3:
                row['selection']='selected_by_reviewer_rule'
                selected.append(row)
            else: row['selection']='after_first_three'
        row['exclusion_reason']=reason
        rows.append(row)
    checksum = 'sha256:'+hashlib.sha256(__import__('app.jobs_v22.digest',fromlist=['canonical_json_bytes']).canonical_json_bytes(snapshot)).hexdigest()
    for row in selected:
        row.update(source_market_checksum=checksum, source_market_job_id=str(snapshot.job_id),
                   final_discovery_binding='pending_complete_identity_and_workflow')
    return rows, selected


async def run():
    previous_checksums=json.loads((PRIOR/'checksums.json').read_text())
    for name, checksum in previous_checksums.items():
        if hashlib.sha256((PRIOR/name).read_bytes()).hexdigest()!=checksum: raise RuntimeError('prior_evidence_changed')
    previous_events=[json.loads(p.read_text()) for p in sorted((PRIOR/'attempts').glob('attempt-*.json'))]
    if len(previous_events)!=json.loads((PRIOR/'summary.json').read_text())['serpapi_attempts']: raise RuntimeError('prior_budget_mismatch')
    env=dotenv_values(ROOT/'.env'); names=('SERPAPI_KEY','SERPAPI_KEY_SECONDARY','SERPAPI_KEY_TERTIARY')
    values={n:env.get(n) or '' for n in names};keys=configured_serpapi_keys(*values.values())
    secrets=[*keys,env.get('FIRECRAWL_API_KEY') or '']
    if not keys: raise RuntimeError('provider_keys_unavailable')
    OUTPUT.mkdir(exist_ok=False); attempts=OUTPUT/'attempts';attempts.mkdir()
    budget=Budget(attempts,secrets);budget.events=previous_events.copy()
    def save(path, value):
        with path.open('x') as file:file.write(safe_text(value,secrets))
    settings=Settings(_env_file=None,**values)
    stage=build_serp_market_stage(settings)
    redis=fakeredis.aioredis.FakeRedis(); cp=JobCheckpoints(redis,prefix='p009-adjudication',ttl_seconds=86400)
    points={}; point_audit=[]; results=[]
    try:
        # One city-only lookup per distinct city; explicit Hilo is authorized.
        for site in SITES:
            city=site['city']
            if city in points:continue
            audit=dict(city=city,reference=REFERENCES[city],max_distance_km=8)
            if city=='Hilo':
                point=REFERENCES[city];audit.update(source='reviewer_explicit_coordinates',distance_km=0,accepted=True)
            else:
                await budget.claim('serpapi_city_location')
                lookup=await stage.location_provider.search(city)
                candidates=[]
                for item in lookup.items:
                    gps=item.get('gps')
                    if item.get('country_code')!=site['country'] or str(item.get('target_type')).casefold()!='city' or str(item.get('name')).casefold()!=city.casefold():continue
                    if not isinstance(gps,list) or len(gps)!=2:continue
                    coord=(gps[1],gps[0]); km=distance(coord,REFERENCES[city])
                    candidates.append(dict(location=item,coordinates=coord,distance_km=km,accepted=km<=8))
                audit.update(source='provider_city_only',lookup_checksum=lookup.response_checksum,candidates=candidates)
                good=[c for c in candidates if c['accepted']]
                unique={tuple(c['coordinates']) for c in good}
                if len(unique)!=1:
                    point=None;audit.update(accepted=False,reason='city_point_unresolved_or_ambiguous')
                else:
                    point=tuple(good[0]['coordinates']);audit.update(accepted=True,distance_km=good[0]['distance_km'])
            points[city]=point;point_audit.append(audit)
        save(OUTPUT/'city-point-audit.json',point_audit)
        nova=dict(SITES[0],id='nova-service',name='Nova Service',url='https://novaservice.pro/')
        # A fresh Nova market observation supplies the same city point without
        # changing any frozen Nova v5 report or source bindings.
        for site in [*SITES,nova]:
            folder=OUTPUT/site['id'];folder.mkdir();start=len(budget.events)
            result=dict(site=site,started_at=now(),status='stopped_after_adjudication')
            if site['id']=='nova-service':
                result['gbp_status']='not_requeried_existing_frozen_report'
            else:
                old=json.loads((PRIOR/site['id']/'candidates.json').read_text())
                signals=extract_site_signals((ROOT/'tests/fixtures/gold'/site['id']/'page.html').read_text())
                phones=[p.value for p in signals.phones]
                result['page_phone_observations']=phones
                if site['id'] in ACCEPTED:
                    accepted=next(c for c in old['gbp_lookup']['candidates'] if (c.get('public_gbp_url') or '').endswith(ACCEPTED[site['id']]))
                    check=matching(accepted,site,phones)
                    result.update(gbp_status='confirmed_by_reviewer' if check['accepted'] else 'gbp_not_found',
                                  gbp=accepted if check['accepted'] else None,identity_check=check,
                                  rejected_prior_candidates=[c for c in old['gbp_lookup']['candidates'] if c!=accepted])
                else:
                    searches=[]; matches={}; new_attempts=0
                    # Prior site contains one GBP search. Cap the entire site's
                    # lookup attempts at three including that original request.
                    remaining=3-1
                    for query in QUERIES[site['id']]:
                        if new_attempts>=remaining:break
                        async def provider(_params):
                            async def before(slot,fingerprint):
                                nonlocal new_attempts
                                if new_attempts>=remaining:raise RuntimeError('site_gbp_lookup_cap')
                                await budget.claim('serpapi_gbp_bounded_lookup');new_attempts+=1
                            params=dict(engine='google_maps',type='search',q=query,gl=site['country'].lower(),hl='en')
                            if points.get(site['city']):params['ll']='@'+','.join(str(x) for x in points[site['city']])+',14z'
                            async with httpx.AsyncClient(timeout=40,trust_env=False,follow_redirects=False) as client:
                                response=await execute_serpapi_get(client,params,keys=keys,base_url='https://serpapi.com/search',before_attempt=before)
                            return response.payload
                        found=await LimitedGbpLookup(provider=provider,configured=True).lookup(site_url=site['url'],signals=signals)
                        entry=dict(query=query,status=found.status,candidates=[])
                        for candidate in found.candidates:
                            c=asdict(candidate);check=matching(c,site,phones);entry['candidates'].append(dict(candidate=c,identity_check=check))
                            if check['accepted'] and c.get('public_gbp_url'):matches[c['public_gbp_url']]=c
                        searches.append(entry)
                        if matches:break
                    result.update(gbp_searches=searches,gbp_lookup_attempts_this_phase=new_attempts,
                                  gbp_lookup_attempts_cumulative=1+new_attempts,
                                  gbp_status='confirmed_by_matching_rule' if len(matches)==1 else 'gbp_ambiguous' if matches else 'gbp_not_found',
                                  gbp=next(iter(matches.values())) if len(matches)==1 else None)
            point=points[site['city']]
            if point is None:result['market_status']='city_point_unresolved_no_requery'
            else:
                label='Hilo, Hawaii, United States' if site['city']=='Hilo' else ', '.join(x for x in [site['city'],site['region'],site['country']] if x)
                context=SerpMarketContext(target_market=dict(display_name=label,city=site['city'],region=site['region'],country_code=site['country'],latitude=point[0],longitude=point[1]),queries=site['queries'])
                try:
                    snapshot=await stage.collect_context(job_id=uuid4(),context=context,checkpoints=cp,cost_ledger=budget)
                    save(folder/'market-snapshot.json',snapshot.model_dump(mode='json'))
                    from urllib.parse import parse_qs, urlsplit
                    gbp_url=(result.get('gbp') or {}).get('public_gbp_url') or ''
                    own_cid=ACCEPTED.get(site['id']) or (parse_qs(urlsplit(gbp_url).query).get('cid') or [None])[0]
                    rows,selected=select_maps(snapshot,site,own_cid)
                    result.update(market_status='recollected_at_city_point',coordinates=point,q1_maps=rows,selected_competitors=selected)
                except Exception as exc:
                    result.update(market_status='unavailable',market_error_type=type(exc).__name__,market_error_code=getattr(exc,'error_code',None))
            result.update(completed_at=now(),attempts_this_site=len(budget.events)-start)
            save(folder/'result.json',result)
            summary=dict(site_id=site['id'],gbp_status=result.get('gbp_status'),market_status=result['market_status'],selected_competitors=len(result.get('selected_competitors',[])),attempts_this_site=result['attempts_this_site'])
            results.append(summary);print(json.dumps(summary),flush=True)
        summary=dict(status='STOPPED_AFTER_ADJUDICATION',sites=results,prior_attempts=len(previous_events),
                     additional_attempts=len(budget.events)-len(previous_events),serpapi_attempts_cumulative=len(budget.events),
                     firecrawl_credits_cumulative=0,usd_usage_reserve=str(Decimal(len(budget.events))*Decimal('.025')),
                     remaining_serpapi_attempts=185-len(budget.events),remaining_firecrawl_credits=30,
                     remaining_usd_reserve=str(Decimal('5')-Decimal(len(budget.events))*Decimal('.025')),actual_invoice_usd=None)
        save(OUTPUT/'summary.json',summary)
        checksums={str(p.relative_to(OUTPUT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUTPUT.rglob('*')) if p.is_file()}
        save(OUTPUT/'checksums.json',checksums)
        print(safe_text(summary,secrets),flush=True)
    finally:await redis.aclose()


def self_test():
    assert distance(REFERENCES['Fresno'],REFERENCES['Fresno'])==0
    assert distance((36.314551,-119.311721),REFERENCES['Fresno'])>8
    assert matching(dict(website_url='https://visiontek.com',phone='(866) 883-5411'),SITES[2],['0333 335 5510'])['accepted'] is False
    assert matching(dict(website_url='http://visiontechav.com'),SITES[2],[])['accepted']
    assert matching(dict(phone='+44 333 335 5510'),SITES[2],['0333 335 5510'])['accepted']
    # Exercise selection with source records without constructing a fake identity.
    base=json.loads((PRIOR/'pr-plumbing-fresno/market-snapshot.json').read_text())
    from app.collectors.serp_market_models import SerpMarketSnapshot
    snap=SerpMarketSnapshot.model_validate_json(json.dumps(base))
    rows,selected=select_maps(snap,SITES[0])
    assert all(r['result_type']=='maps' for r in rows)
    assert all('Fresno' in r['address'] for r in selected)
    assert len(selected)<=3
    assert all(r['source_market_job_id']==str(snap.job_id) for r in selected)
    import tempfile
    async def carry_budget():
        with tempfile.TemporaryDirectory() as directory:
            budget=Budget(Path(directory),[])
            budget.events=[dict(id=str(uuid4())) for _ in range(34)]
            for _ in range(151):await budget.claim('test')
            try:await budget.claim('test')
            except RuntimeError:pass
            else:raise AssertionError('Cumulative budget reset')
            assert len(list(Path(directory).glob('attempt-*.json')))==151
    asyncio.run(carry_budget())
    print('PASS: 8km gate, wrong-domain rejection, domain/phone OR match, maps-only city filtering, snapshot references, cumulative budget carry-forward')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--self-test',action='store_true');args=parser.parse_args()
    try:
        if args.self_test:self_test()
        else:asyncio.run(run())
    except Exception as exc:
        print(json.dumps(dict(status='STOPPED',error_type=type(exc).__name__)),flush=True)
        raise SystemExit(1) from None
