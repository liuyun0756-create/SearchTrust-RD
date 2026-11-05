"""Saved P0-09 regressions + synthetic boundary cases. No network permitted."""
import asyncio
import hashlib
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.jobs_v22.customer_public_gbp_stage import (
    CustomerPublicGbpProviderResponse, SerpApiCustomerPublicGbpProvider,
)
from app.preflight_v22.gbp_identity import (
    IdentityGbpLookup, assess_candidate, build_identity_gbp_lookup, canonical_host,
    exclude_customer_from_competitors, record_from_saved_mapping,
    observations_from_site_selection,
)
from app.preflight_v22.gbp_identity_models import (
    GbpIdentityRequest, GbpIdentityResult, GbpLookupLedger, IdentityObservation,
    LookupAttempt, SavedGbpRecord, VerifiedAlias,
)

SAVED = json.loads((Path(__file__).parent / "fixtures/p1-05-gbp/saved-candidates.json").read_text())
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("network forbidden in saved-data GBP tests")
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)


def saved_records(site, section):
    fixture = SAVED[site]
    part = fixture[section]
    rows = [part['record']] if section == 'confirmed' else part['records']
    return [record_from_saved_mapping(row,
        source_ref=part['source'] + part['selector'], record_id=row.get('record_id', str(i)),
        payload_checksum=fixture['provenance'][part['source']],
        observed_at=datetime.fromisoformat(part['observed_at']),
        expires_at=datetime.fromisoformat(part['expires_at']) if part.get('expires_at') else None,
        kind='market' if section == 'market' else 'lookup') for i, row in enumerate(rows) if row]


def request_for(site='vision-tech-swansea', **updates):
    fixture = SAVED[site]
    site_data = fixture['site']
    values = dict(case_ref=site, site_url=site_data['url'], country_code=site_data['country'],
        target_market=dict(display_name=site_data['city'], city=site_data['city'], country_code=site_data['country']),
        evaluated_at=NOW, market_collected=True,
        # The old regex scan also copied a number from an executable Divi
        # script. page.html has no visible/tel element for that number; it is
        # not an owned contact observation. Production takes scoped observations.
        phones=[dict(value=p, page_url=site_data['url'],
                     belongs_to_business=not (site == 'seamless-gutters-pensacola' and p == '8504557246'),
                     source_ref=fixture['market']['source'] + '/page_phone_observations')
                for p in fixture['phone_observations']],
        seo_business_name=site_data['name'], seo_name_source_ref='P0-09 approved inputs')
    values.update(updates)
    return GbpIdentityRequest(**values)


def record(**updates):
    values = dict(source_ref='synthetic source', record_id='1', payload_checksum=DIGEST,
        observed_at=NOW - timedelta(hours=1), expires_at=NOW + timedelta(hours=1), kind='lookup',
        business_name='Vision Tech AV LTD', website_url='https://visiontechav.com/',
        cid='3467016953362037400', phone='0333 335 5510', address='Unit 9, Cross Hands')
    values.update(updates)
    return SavedGbpRecord(**values)


class SavedProvider:
    def __init__(self, *payloads):
        self.payloads = iter(payloads)
        self.calls = []

    async def request(self, params, *, before_attempt, after_attempt):
        await before_attempt(1, 'mock fingerprint')
        self.calls.append(params)
        payload = next(self.payloads)
        if isinstance(payload, Exception):
            raise payload
        await after_attempt(1, 'mock fingerprint', None)
        checksum = 'sha256:' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return CustomerPublicGbpProviderResponse(payload, checksum)


@pytest.mark.anyio
async def test_seamless_reuses_real_market_brand_after_fifth_position_without_provider():
    rows = saved_records('seamless-gutters-pensacola', 'market')
    index = next(i for i,r in enumerate(rows) if r.cid == '16921898606511158364')
    assert index >= 5
    req = request_for('seamless-gutters-pensacola', saved_records=rows)
    result = await IdentityGbpLookup(provider=SavedProvider()).lookup(req)
    assert (result.status, result.selected.cid, result.selected.business_name) == (
        'matched', '16921898606511158364', 'Gutter Solutions & Home Improvements')
    chosen = next(c for c in result.candidates if c.record == result.selected)
    assert chosen.domain_match is True and chosen.phone_match is True
    assert result.ledger.attempts == [] and result.visited_stages == ['cache']
    competitors = exclude_customer_from_competitors([result.selected, *rows], req, result)
    assert len(competitors) == 3 and all(r.cid != result.selected.cid for r in competitors)


@pytest.mark.anyio
async def test_vision_rejects_two_real_us_companies_and_uses_saved_correct_business():
    wrong = saved_records('vision-tech-swansea', 'initial')
    req = request_for(saved_records=wrong)
    result = await IdentityGbpLookup().lookup(req)
    assert result.status == 'lookup_not_checked'  # paid switch off != search completed
    assert all(not c.eligible and c.domain_match is False for c in result.candidates)
    # US national formatting is incomparable in the site's reliable GB context;
    # unknown is never a positive match or a fabricated explicit mismatch.
    assert all(c.phone_match is not True for c in result.candidates)
    req.saved_records += saved_records('vision-tech-swansea', 'confirmed')
    result = await IdentityGbpLookup().lookup(req)
    assert result.selected.cid == '3467016953362037400'
    assert result.selected.business_name == 'Vision Tech AV LTD'
    assert not result.ledger.attempts


@pytest.mark.anyio
async def test_volcanoes_two_saved_cids_require_explicit_selection_and_cannot_bypass_gate():
    rows = saved_records('volcanoes-hiking-guides', 'initial')
    req = request_for('volcanoes-hiking-guides', saved_records=rows)
    result = await IdentityGbpLookup().lookup(req)
    assert result.status == 'identity_unconfirmed' and result.reason == 'multiple_verified_branches'
    assert result.selected is None
    req.selected_branch_ref = 'cid:16906567364961418758'
    req.selection_source_ref = 'reviews/P0-09.md/候选确认与采样点裁定'
    result = await IdentityGbpLookup().lookup(req)
    assert result.selected.cid == '16906567364961418758' and not result.ledger.attempts
    req.saved_records = saved_records('vision-tech-swansea', 'initial')
    result = await IdentityGbpLookup().lookup(req)
    assert result.selected is None and all(not c.eligible for c in result.candidates)


@pytest.mark.anyio
@pytest.mark.parametrize('site', ['seamless-gutters-pensacola', 'volcanoes-hiking-guides'])
async def test_saved_shortlink_address_placeholder_is_never_a_business_even_if_selected(site):
    # Actual P0-09 placeholder is Seamless, reused as a cross-site rejection
    # boundary for Volcanoes; not claimed to be a Volcanoes provider response.
    rows = saved_records('seamless-gutters-pensacola', 'initial')
    req = request_for(site, saved_records=rows, selected_branch_ref='cid:10354290509108082871',
        selection_source_ref='synthetic selection of saved address',
        gbp_url='https://maps.app.goo.gl/8MMJyNe2na4og54U7',
        saved_link_destinations={'https://maps.app.goo.gl/8MMJyNe2na4og54U7':
                                 'https://www.google.com/maps?cid=10354290509108082871'})
    result = await IdentityGbpLookup().lookup(req)
    assert (result.status, result.reason, result.selected) == ('identity_unconfirmed', 'address_only_place', None)
    assert result.lookup_stop_reason == 'incremental_lookup_disabled'
    assert not result.candidates[0].eligible and not result.ledger.attempts


@pytest.mark.parametrize(('url','expected'), [
    ('https://WWW.VISIONTECHAV.com./swansea/', 'visiontechav.com'),
    ('https://www.bücher.de/', 'xn--bcher-kva.de'), ('http://a.example.com/', 'a.example.com'),
    ('https://visiontechav.com@evil.example/', None), ('not-a-url', None),
])
def test_canonical_domains(url, expected):
    assert canonical_host(url) == expected


@pytest.mark.parametrize(('website','phone','domain','number','eligible'), [
    ('https://www.visiontechav.com/path', None, True, None, True),
    ('https://other.example/', '0333 335 5510', False, True, True),
    ('https://visiontechav.com/', '+1 866 883 5411', True, False, True),
    ('https://visiontechav.com.evil.example', '+1 866 883 5411', False, False, False),
    ('https://visiontechav.co.uk', '5510', False, None, False),
    (None, None, None, None, False),
])
def test_dp_gate_and_conflicts(website, phone, domain, number, eligible):
    decision = assess_candidate(request_for(), record(website_url=website, phone=phone))
    assert (decision.domain_match, decision.phone_match, decision.eligible) == (domain, number, eligible)
    if domain is False: assert 'domain_conflict' in decision.reasons
    if number is False: assert 'phone_conflict' in decision.reasons


@pytest.mark.parametrize('change', [dict(purpose='privacy'), dict(purpose='review'),
    dict(purpose='competitor'), dict(belongs_to_business=False), dict(page_url='https://evil.example/')])
def test_phone_cannot_be_borrowed_from_unowned_or_third_party_content(change):
    req = request_for()
    req.phones = [req.phones[0].model_copy(update=change)]
    assert not assess_candidate(req, record(website_url=None)).eligible


def test_unknown_country_and_cross_tenants_and_verified_alias():
    req = request_for(country_code=None)
    assert assess_candidate(req, record(website_url=None)).phone_match is None
    req = request_for(site_url='https://a.example.com', phones=[])
    assert not assess_candidate(req, record(website_url='https://b.example.com')).eligible
    req.aliases = [VerifiedAlias(url='https://b.example.com', source_ref='verified redirect record', method='saved_redirect')]
    decision = assess_candidate(req, record(website_url='https://b.example.com'))
    assert decision.eligible and decision.matched_source_refs == ['verified redirect record']


@pytest.mark.anyio
async def test_complete_lookup_order_link_domain_phone_then_explicit_name():
    provider = SavedProvider({'place_results': SAVED['seamless-gutters-pensacola']['initial']['records'][0]},
        {'local_results': SAVED['vision-tech-swansea']['initial']['records']},
        {'local_results': [SAVED['vision-tech-swansea']['confirmed']['record']]})
    req = request_for(gbp_url='https://www.google.com/maps?cid=10354290509108082871')
    result = await IdentityGbpLookup(provider=provider, incremental_enabled=True, clock=lambda: NOW).lookup(req)
    assert result.selected.cid == '3467016953362037400'
    assert [a.stage for a in result.ledger.attempts] == ['link', 'domain_phone', 'name']
    assert provider.calls[0]['data_cid'] == '10354290509108082871'
    assert provider.calls[1]['q'] == 'visiontechav.com +443333355510 Swansea GB'
    assert provider.calls[2]['q'] == 'Vision-Tech AV Swansea GB'
    assert all(a.outcome == 'success' for a in result.ledger.attempts)
    assert GbpIdentityResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.anyio
async def test_missing_market_never_buys_query_even_with_enabled_switch():
    provider = SavedProvider()
    result = await IdentityGbpLookup(provider=provider, incremental_enabled=True).lookup(request_for(market_collected=False))
    assert result.reason == 'awaiting_market_cache' and provider.calls == []
    # A repeat after the original market stage reuses saved rows for zero calls.
    req = request_for('seamless-gutters-pensacola', saved_records=saved_records('seamless-gutters-pensacola','market'))
    assert (await IdentityGbpLookup(provider=provider, incremental_enabled=True).lookup(req)).status == 'matched'
    assert provider.calls == []


@pytest.mark.anyio
async def test_default_config_off_and_cache_works_without_any_provider():
    settings = Settings(_env_file=None)
    assert settings.V22_IDENTITY_GBP_INCREMENTAL_ENABLED is False
    service = build_identity_gbp_lookup(settings)
    assert service.provider is None
    result = await service.lookup(request_for())
    assert (result.status, result.reason) == ('lookup_not_checked', 'incremental_lookup_disabled')


@pytest.mark.anyio
async def test_only_complete_successful_search_can_return_not_found():
    provider = SavedProvider({'local_results': SAVED['vision-tech-swansea']['initial']['records']}, {'local_results': []})
    result = await IdentityGbpLookup(provider=provider, incremental_enabled=True, clock=lambda: NOW).lookup(request_for())
    assert (result.status, result.reason) == ('gbp_not_found', 'no_verified_domain_or_phone_match')
    assert len(result.ledger.attempts) == 2 and result.selected is None


@pytest.mark.anyio
@pytest.mark.parametrize('payload', [RuntimeError('api_key=do-not-print'), {}, {'error':'secret'},
    {'search_metadata':{'status':'Processing'},'local_results':[]}, {'local_results':None}])
async def test_failure_or_malformed_response_is_not_checked_without_exception_leak(payload):
    result = await IdentityGbpLookup(provider=SavedProvider(payload), incremental_enabled=True).lookup(request_for())
    assert result.status == 'lookup_not_checked' and result.selected is None
    assert 'secret' not in result.model_dump_json() and 'api_key' not in result.model_dump_json()


@pytest.mark.anyio
async def test_expired_cache_is_a_clue_not_current_fact_or_fresh_timestamp():
    old = record(expires_at=NOW - timedelta(seconds=1))
    req = request_for(saved_records=[old])
    result = await IdentityGbpLookup().lookup(req)
    assert result.selected is None and not result.candidates[0].current
    assert result.candidates[0].record.observed_at == old.observed_at
    provider = SavedProvider({'place_results':SAVED['vision-tech-swansea']['confirmed']['record']})
    result = await IdentityGbpLookup(provider=provider, incremental_enabled=True, clock=lambda: NOW).lookup(req)
    assert result.status == 'matched' and provider.calls[0]['data_cid'] == old.cid
    assert result.ledger.attempts[0].stage == 'refresh'
    assert old.observed_at == NOW - timedelta(hours=1)


@pytest.mark.anyio
async def test_duplicates_do_not_create_branch_ambiguity_and_source_is_preserved():
    req = request_for(saved_records=[record(), record()])
    result = await IdentityGbpLookup().lookup(req)
    assert result.status == 'matched' and len(result.candidates) == 1


@pytest.mark.anyio
async def test_real_transport_key_rotation_counts_every_attempt_and_stops_at_three():
    sent = []
    def respond(request):
        sent.append(request)
        return httpx.Response(401, json={'error':'Invalid API key'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = SerpApiCustomerPublicGbpProvider(keys=['fake1','fake2','fake3'],
            base_url='https://serpapi.example/search', connect_timeout=1, read_timeout=1,
            total_timeout=2, max_response_bytes=10000, client_factory=lambda:client)
        # One already-spent request + two retry attempts: the third key is blocked.
        ledger = GbpLookupLedger(case_ref='vision-tech-swansea', normalized_domain='visiontechav.com',
            attempts=[LookupAttempt(number=1, stage='link', outcome='failed')])
        result = await IdentityGbpLookup(provider=provider, incremental_enabled=True).lookup(request_for(), ledger=ledger)
        assert len(sent) == 2 and len(result.ledger.attempts) == 3
        assert result.reason == 'extra_request_budget_exhausted' and result.status == 'lookup_not_checked'
        again = await IdentityGbpLookup(provider=provider, incremental_enabled=True).lookup(request_for(), ledger=result.ledger)
        assert len(sent) == 2 and again.reason == 'extra_request_budget_exhausted'
        assert 'fake1' not in result.model_dump_json()


@pytest.mark.anyio
async def test_ledger_is_bound_to_case_domain_and_budget_counts_details():
    ledger = GbpLookupLedger(case_ref='other', normalized_domain='visiontechav.com')
    with pytest.raises(ValueError, match='another Case'):
        await IdentityGbpLookup().lookup(request_for(), ledger=ledger)
    old = [record(cid=str(i), record_id=str(i), expires_at=NOW-timedelta(seconds=1)) for i in range(4)]
    provider = SavedProvider(*[{'local_results': []} for _ in range(3)])
    service = IdentityGbpLookup(provider=provider, incremental_enabled=True, clock=lambda:NOW)
    req = request_for(saved_records=old)
    result = await service.lookup(req)
    assert len(provider.calls) == 3 and result.reason == 'extra_request_budget_exhausted'
    repeated = await service.lookup(req)
    assert repeated.reason == 'extra_request_budget_exhausted' and len(provider.calls) == 3
    with pytest.raises(ValueError, match='cannot discard'):
        await service.lookup(req, ledger=GbpLookupLedger(case_ref=req.case_ref, normalized_domain='visiontechav.com'))


@pytest.mark.anyio
async def test_timeout_is_counted_and_does_not_continue_to_next_query():
    class Slow:
        async def request(self, params, *, before_attempt, after_attempt):
            await before_attempt(1,'fake')
            await asyncio.sleep(1)
    result = await IdentityGbpLookup(provider=Slow(), incremental_enabled=True, timeout_seconds=.001).lookup(request_for())
    assert result.status == 'lookup_not_checked' and len(result.ledger.attempts) == 1
    assert result.ledger.attempts[0].outcome == 'interrupted'


@pytest.mark.anyio
async def test_no_domain_derived_name_and_no_live_shortlink_expansion():
    provider = SavedProvider({'local_results': []})
    req = request_for(seo_business_name=None, seo_name_source_ref=None, gbp_url='https://maps.app.goo.gl/unknown')
    result = await IdentityGbpLookup(provider=provider, incremental_enabled=True, clock=lambda: NOW).lookup(req)
    assert len(provider.calls) == 1 and result.visited_stages == ['cache','saved_link','domain_phone']


def test_frozen_truth_not_used_by_discovery():
    import inspect
    import app.preflight_v22.gbp_identity as module
    assert 'truth.json' not in inspect.getsource(module)


def test_existing_extractor_bridge_ignores_script_and_unowned_phone_without_gbp_bootstrap():
    from site_business_helpers import request, business_html
    from app.report_v22.site_business_facts import build_site_business_facts
    from app.report_v22.site_business_selection import select_site_business_candidates
    from app.report_v22.models import BusinessIdentity
    html = business_html(url='https://example.test/', telephone='+1 850 776 1782')
    html += '<script>var data = [{"url":"tel:8504557246"}];</script>'
    html += business_html(url='https://competitor.test/', telephone='+1 850 455 7246')
    value = request(html)
    identity = BusinessIdentity(business_name='Fixture Plumbing', site_url='https://example.test/',
        normalized_domain='example.test', operating_model='service_area',
        primary_location=dict(display_name='Austin', country_code='US'))
    facts = build_site_business_facts(value)
    selected = select_site_business_candidates(facts, identity)
    observations = observations_from_site_selection(facts, selected)
    assert [p.value for p in observations['phones']] == ['+1 850 776 1782']
    assert all(p.belongs_to_business and p.source_ref.startswith('sha256:') for p in observations['phones'])


@pytest.mark.anyio
async def test_saved_link_place_id_reuses_same_entity_without_creating_false_ambiguity():
    req = request_for(saved_records=[record(place_id='ChIJtest'),
        record(cid=None, place_id='ChIJtest', record_id='second')],
        gbp_url='https://google.com/maps?place_id=ChIJtest',
        selected_branch_ref='place_id:ChIJtest', selection_source_ref='explicit saved choice')
    result = await IdentityGbpLookup().lookup(req)
    assert result.status == 'matched' and not result.ledger.attempts


@pytest.mark.anyio
async def test_missing_comparison_fields_are_not_absence_and_record_with_address_title_fails_even_with_domain():
    provider = SavedProvider({'local_results': [{'title':'Unverified brand', 'cid':'123'}]}, {'local_results': []})
    result = await IdentityGbpLookup(provider=provider, incremental_enabled=True, clock=lambda:NOW).lookup(request_for())
    assert result.status == 'gbp_not_found'
    assert 'website_unavailable_or_invalid' in result.candidates[0].reasons
    assert 'phone_unavailable_or_incomparable' in result.candidates[0].reasons
    c = assess_candidate(request_for(), record(business_name='Unit 9', address='Unit 9, Cross Hands'))
    assert c.domain_match and c.phone_match and not c.eligible
