import pytest
from support.private_gold import require_private_gold_inputs
from app.report_v22.opening_hours import parse_week, parse_spec, compare_candidates

@pytest.mark.parametrize('text,category', [
 ('Opening Hours Mon-Fri 8-5','parse_ambiguous'),
 ('Opening Hours Mon-Fri 09:00-17:30','parsed'),
 ('Monday 9 am-5 pm','parsed'),
 ('Monday Open 24 hours','parsed'),
 ('Open daily 8:00 AM–5:00 PM','parsed'),
 ('Emergency Services Available 24/7/365','emergency'),
 ('Group tour Friday at 9 am and 11 am','tour_schedule'),
 ('Hikes from 1.5 to 6 hours','tour_schedule'),
 ('Sat-Sun Appointment Only','appointment_only'),
 ('Monday 00:00-00:00','parse_ambiguous'),
 ('Monday 9-5 pm','parse_ambiguous'),
 ('Monday 09:00-17:00 PST','parse_ambiguous'),
 ('Christmas Monday 09:00-17:00','other'),
])
def test_explicit_formats_and_purposes(text,category):
 assert parse_week(text)['classification']==category

def test_unknown_days_lunch_overnight_and_full_day():
 row=parse_week('Mon-Fri 09:00-12:00, 13:00-17:00')
 assert row['days']=={str(i):[[540,720],[780,1020]] for i in range(5)}
 row=parse_week('Sunday 22:00-02:00')
 assert row['days']=={'6':[[1320,1440]],'0':[[0,120]]}
 assert parse_week('Monday 12 am-12 pm')['days']['0']==[[0,720]]
 assert parse_spec({'dayOfWeek':'Monday','opens':'00:00','closes':'23:59'})['days']=={'0':[[0,1440]]}
 assert parse_week('Monday Closed')['days']=={'0':[]}

def test_partial_comparison_and_appointment_are_not_guessed():
 weekdays=parse_week('Mon-Fri 09:00-17:30')
 all_day=parse_week('Open 24/7')
 assert compare_candidates([weekdays],[all_day])['state']=='mismatch'
 assert compare_candidates([weekdays],[weekdays])['state']=='partial_match'
 appointment=parse_week('Sat-Sun Appointment Only')
 assert compare_candidates([weekdays,appointment],[all_day])['state']=='not_checked'
 assert compare_candidates([weekdays],[all_day],eligible=False)['state']=='not_checked'
 assert compare_candidates([weekdays,parse_week('Mon-Fri 10:00-18:00')],[all_day])['reason']=='conflicting_hours_sources'

@pytest.mark.parametrize('left,right',[
 ('Group tour Friday 9 am-11 am','Open daily 9 am-5 pm'),
 ('Emergency 24/7','Open 24/7'),
 ('Mon-Fri 8-5','Open 24/7'),
 ('Monday 00:00-00:00','Open 24/7'),
 ('Monday 09:00-17:00','Tuesday 09:00-17:00'),
 ('Holiday Monday 09:00-17:00','Open 24/7')])
def test_unchecked_never_becomes_missing_or_mismatch(left,right):
 assert compare_candidates([parse_week(left)],[parse_week(right)])['state']=='not_checked'

def test_gbp_alias_conflict_and_ambiguous_alias_are_not_silently_dropped():
 site=[parse_week('Open daily 9 am-5 pm')]
 assert compare_candidates(site,[parse_week('Open 24/7'),parse_week('Open daily 9 am-5 pm')])['reason']=='conflicting_hours_sources'
 assert compare_candidates(site,[parse_week('Open 24/7'),parse_week('Mon-Fri 8-5')])['state']=='not_checked'


def test_real_contact_snapshot_compares_only_against_still_valid_gbp():
 import json,gzip,hashlib
 from pathlib import Path
 from datetime import datetime
 from app.report_v22.findings_models import PublicFindingsInput
 from app.report_v22.evidence_bindings import eligibility
 from app.report_v22.opening_hours import extract_page,gbp_candidates,ParseBudget,SiteBusinessLimits
 from types import SimpleNamespace
 root=Path(__file__).resolve().parents[1]
 batch=root/'tests/fixtures/p1-00b-contact/volcanoes'
 capture=json.loads((batch/'capture-resumed.json').read_text())
 now=datetime.fromisoformat(capture['completed_at'].replace('Z','+00:00'))
 body=gzip.decompress((batch/'contact.html.gz').read_bytes())
 assert any(r.get('sha256')=='sha256:'+hashlib.sha256(body).hexdigest() for r in capture['responses'])
 bound=json.loads(gzip.decompress((root/'tests/fixtures/gold/volcanoes-hiking-guides/bound-p0-10.json.gz').read_bytes()))
 request=PublicFindingsInput.model_validate_json(json.dumps(bound['request']))
 gbp=next(s for s in request.evidence_input.sources if s.kind=='public_gbp')
 assert eligibility(gbp,now) is None and gbp.binding.fetched_at<=now<gbp.binding.expires_at
 limits=SiteBusinessLimits().model_dump();limits.update(max_html_bytes_per_page=4000000,max_html_bytes_total=10000000)
 left=extract_page(body.decode(),url='https://www.volcanoeshikingguides.com/contact-us',path='/html',identity=request.business_identity,budget=ParseBudget(SimpleNamespace(**limits)))
 result=compare_candidates(left,gbp_candidates(gbp),eligible=eligibility(gbp,now) is None)
 assert result['state']=='exact_match' and len(result['compared_days'])==7
 assert any('Every day, 8:00 AM–5:00 PM' in r['raw'] for r in left)
 assert all(r['days']=={str(i):[[480,1020]] for i in range(7)} for r in left if r['classification']=='parsed')


@pytest.mark.parametrize('name', ['all-star-plumbing-fresno', 'vision-tech-swansea', 'nova-service'])
def test_real_v11_hours_fixtures_are_actual_generation_and_new_targets_are_unlabelled(monkeypatch, name):
 from test_p0_08_gold import GOLD,ROOT,deny_network
 from scripts.eval_gold import generate,read_json,compare_findings
 from app.jobs_v22.digest import canonical_json_bytes
 from app.report_v22.public_rule_catalog import HOURS
 deny_network(monkeypatch)
 directory=GOLD/name
 require_private_gold_inputs(directory, ROOT.parent/'.release-private/2026-10-29')
 report,result,*_=generate(read_json(directory/'input.json'),directory,ROOT.parent/'.release-private/2026-10-29',ruleset='v22_capability_public_v11')
 audit=result._hours_alignment
 if name=='nova-service':
  assert audit['state']=='not_checked'
  assert not any(f.rule_id==HOURS for f in report.findings)
  return
 assert audit['state']=='mismatch'
 assert canonical_json_bytes(report)+b'\n'==(ROOT/'tests/fixtures/p1-00b-hours'/f'{name}.json').read_bytes()
 assert audit['basis']==('structured' if name.startswith('all-star') else 'text_pattern')
 assert len(audit['compared_days'])==(7 if name.startswith('all-star') else 5)
 rows=compare_findings(read_json(directory/'truth.json'),[f.model_dump(mode='json') for f in report.findings],[e.model_dump(mode='json') for e in result.rule_evaluations])
 assert all(r['verdict']!='正确' for r in rows if r.get('rule_id')==HOURS)


def test_overnight_conflicting_next_day_and_unknown_conditions_remain_unchecked():
 assert parse_week('Monday 22:00-02:00 Tuesday Closed')['classification']=='parse_ambiguous'
 assert parse_week('Monday 22:00-02:00 Tuesday 09:00-17:00')['classification']=='parse_ambiguous'
 assert parse_week('Monday 09:00-17:00 except special events')['classification']=='parse_ambiguous'
 assert parse_week('24 hours a day, 7 days a week')['days']=={str(i):[[0,1440]] for i in range(7)}


def test_hidden_malformed_resource_limited_and_unowned_hours_are_not_comparable():
 from app.report_v22.opening_hours import extract_page,ParseBudget,SiteBusinessLimits
 from types import SimpleNamespace
 identity=SimpleNamespace(site_url='https://example.com/')
 def extract(html):return extract_page(html,url=identity.site_url,path='/html',identity=identity,budget=ParseBudget(SiteBusinessLimits()))
 for html in ('<p>Closed-toe hiking shoes. Sun protection. National Park hikes.</p>', '<div style="display:none"><p>Opening Hours Monday 09:00-17:00</p></div>', '<p hidden>Opening Hours Monday 09:00-17:00</p>', '<p style="display:none">Opening Hours Monday 09:00-17:00</p>', '<!-- Opening Hours Monday 09:00-17:00 -->'):
  assert not extract(html)
 assert extract('x'*1_000_001)[0]['reason']=='resource_or_parse_limit'
 assert not extract('<div><p>Opening Hours Monday 09:00-17:00')
 rows=extract('<script type="application/ld+json">{"@type":"LocalBusiness","name":"Someone else","url":"https://other.test/","openingHours":"Monday 09:00-17:00"}</script>')
 assert rows and rows[0]['reason']=='identity_unresolved'
 assert compare_candidates(rows,[parse_week('Open 24/7')])['state']=='not_checked'


def test_local_tour_label_is_not_lost_when_time_is_in_a_sibling():
 from app.report_v22.opening_hours import extract_page,ParseBudget,SiteBusinessLimits
 from types import SimpleNamespace
 rows=extract_page('<section><h2>Tour Hours</h2><p>Friday 9 am-11 am</p></section>',url='https://example.com/',path='/html',identity=SimpleNamespace(site_url='https://example.com/'),budget=ParseBudget(SiteBusinessLimits()))
 assert rows and all(r['classification']=='tour_schedule' for r in rows)


def test_structured_split_intervals_preserve_the_lunch_break():
 result=parse_spec([{'dayOfWeek':'Monday','opens':'09:00','closes':'12:00'}, {'dayOfWeek':'Monday','opens':'13:00','closes':'17:00'}])
 assert result['days']=={'0':[[540,720],[780,1020]]}


def test_v11_completion_reference_and_followup_service_accept_new_report_version():
 import json
 from pathlib import Path
 from types import SimpleNamespace
 from datetime import datetime,timezone,timedelta
 from app.report_v22.capability import CapabilityReport
 from app.api.v2.runtime import state_to_public
 from app.report_v22.capability_service import build_service_capability_report
 from test_v22_capability_service import inputs
 raw=(Path(__file__).parent/'fixtures/p1-00b-hours/all-star-plumbing-fresno.json').read_text()
 report=CapabilityReport.model_validate_json(raw);now=datetime.now(timezone.utc)
 state=SimpleNamespace(report=report,job_id=report.id,case_id=report.input_manifest.case_id,
   revision=3,run_generation=1,message='Analysis complete.',created_at=now-timedelta(minutes=1),deadline_at=now+timedelta(minutes=10),updated_at=now)
 assert state_to_public(state).report_reference.schema_version=='2.2.6'
 manifest,request,previous=inputs(mode='after_acquisition',public_ruleset='v22_capability_public_v11')
 assert previous['schema_version']=='2.2.6'
 followup=build_service_capability_report(manifest,request,previous_report=previous)
 assert followup.report_type=='verified_execution'
 assert str(followup.input_manifest.previous_report.report_id)==previous['id']


def test_current_acquisition_binds_v3_inventory_and_old_rules_reject_it():
 import json
 from dataclasses import replace
 from app.collectors.site_inventory_models import SiteInventorySnapshotV3
 from app.jobs_v22.capability_acquisition_input import build_acquisition_input
 from app.report_v22.capability_public import build_public_capability_report
 from app.report_v22.capability import AnalysisInputManifest
 from app.jobs_v22.digest import verified_request_digest
 from test_v22_capability_acquisition_preparation import inputs
 args,_,_=inputs();raw=args['collected'].site_inventory.model_dump(mode='json');raw['schema_version']='site_inventory_snapshot_v3'
 args['collected']=replace(args['collected'],site_inventory=SiteInventorySnapshotV3.model_validate_json(json.dumps(raw)))
 manifest,request=build_acquisition_input(**args)
 assert manifest.snapshots[0].schema_version=='site_inventory_snapshot_v3'
 assert build_public_capability_report(manifest,request).schema_version=='2.2.7'
 old=manifest.model_dump(mode='json');old['ruleset_version']='v22_capability_public_v10';old['input_checksum']=verified_request_digest({k:v for k,v in old.items() if k!='input_checksum'})
 with pytest.raises(ValueError):AnalysisInputManifest.model_validate_json(json.dumps(old))
