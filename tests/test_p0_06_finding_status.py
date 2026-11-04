import pytest

from app.report_v22.capability_finding_status import PRIOR_PUBLIC_RULESET, alignment_status, gbp_status


@pytest.mark.parametrize("state,expected", [
    ("observed", "confirmed"), ("returned_empty", "not_detected"),
    ("not_returned", "not_checked"), ("unsupported", "not_checked"),
    ("request_failed", "not_checked"),
])
def test_gbp_states_follow_spec_without_inventing_absence(state, expected):
    assert gbp_status(state) == expected


@pytest.mark.parametrize("state,expected", [
    ("exact_match", "confirmed"), ("semantic_match", "confirmed"),
    ("partial_match", "confirmed"), ("mismatch", "confirmed"),
    ("site_missing", "not_detected"), ("not_checked", "not_checked"),
    ("not_applicable", "not_assessed"),
])
def test_alignment_status_is_limited_to_the_actual_compared_scope(state, expected):
    assert alignment_status(state) == expected


@pytest.mark.parametrize("state", ["gbp_missing", "both_missing"])
def test_missing_alignment_state_requires_provider_provenance(state):
    assert alignment_status(state) == "not_checked"
    for provider in ("not_returned", "unsupported", "request_failed", "observed"):
        assert alignment_status(state, gbp_field_state=provider) == "not_checked"
    assert alignment_status(state, gbp_field_state="returned_empty") == "not_detected"


def labelled_public_fixture(*, search_gap=False, ruleset=PRIOR_PUBLIC_RULESET):
    from app.report_v22.capability import AnalysisInputManifest
    from app.report_v22.capability_public import build_public_capability_report
    from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
    from test_v22_capability_public import inputs
    if search_gap:
        from unittest.mock import patch
        from findings_helpers import market
        # A complete, URL-bearing Maps sample with only confirmed competitors.
        # The old helper's "search_gap" is a position gap, not an absent domain.
        sample=market([(f"competitor-{i}.test",i,"provider_position") for i in range(1,4)])
        with patch("test_v22_capability_public.market", return_value=sample):
            manifest, request = inputs([{"status_code":503}],search_gap=True)
    else:
        manifest, request = inputs([{"status_code":503}])
    raw = manifest.model_dump(mode="json")
    raw["ruleset_version"] = ruleset
    raw["input_checksum"] = verified_request_digest({k:v for k,v in raw.items() if k!="input_checksum"})
    manifest = AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw))
    return manifest, request, build_public_capability_report(manifest, request)


def test_new_version_persists_status_kind_and_finding_specific_coverage():
    _, _, report = labelled_public_fixture(search_gap=True)
    assert report.schema_version == "2.2.5"
    assert any(f.rule_id=="v22_public.market_site_domain_unobserved" for f in report.findings)
    for finding in report.findings:
        assert finding.kind and finding.status and finding.coverage
        assert next(c for c in finding.coverage if c.source=="citation").checked is False
        if finding.rule_id=="v22_public.market_site_domain_unobserved":
            assert finding.status=="not_detected" and finding.kind=="fact"
            assert next(c for c in finding.coverage if c.source=="serp").checked is True
            assert next(c for c in finding.coverage if c.source=="site").checked is False
        if finding.rule_id=="v22_public.site_http_error":
            assert finding.status=="confirmed"


@pytest.mark.parametrize("mutation", ["remove", "null", "legacy", "nonfact", "duplicate_source"])
def test_report_rejects_incomplete_or_fabricated_status_metadata(mutation):
    from app.report_v22.capability import CapabilityReport
    from app.jobs_v22.digest import canonical_json_bytes
    from pydantic import ValidationError
    raw=labelled_public_fixture()[2].model_dump(mode="json")
    if mutation=="remove": raw["findings"][0].pop("status")
    if mutation=="null": raw["findings"][0]["coverage"]=None
    if mutation=="legacy": raw["schema_version"]="2.2.2"
    if mutation=="nonfact": raw["findings"][0].update(kind="inference",status="confirmed")
    if mutation=="duplicate_source": raw["findings"][0]["coverage"].append(raw["findings"][0]["coverage"][0])
    with pytest.raises(ValidationError): CapabilityReport.model_validate_json(canonical_json_bytes(raw))


def test_inference_status_is_not_assessed_even_with_checked_sources():
    from app.report_v22.capability_finding_status import annotate_findings
    raw={"evidence":[{"id":"ev", "source_kind":"competitor", "field_path":"/payload/page_type", "value":"service"}],
         "findings":[{"rule_id":"v22_public.competitor_sample_page_type_gap", "evidence_ids":["ev"]}]}
    annotate_findings(raw)
    assert raw["findings"][0]["kind"]=="inference"
    assert raw["findings"][0]["status"]=="not_assessed"


def labelled_service_fixture():
    from app.report_v22.capability import AnalysisInputManifest
    from app.report_v22.capability_service import build_service_capability_report
    from app.report_v22.capability_finding_status import SERVICE_RULESET
    from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
    from test_v22_capability_service import inputs
    manifest, request, previous = inputs()
    raw=manifest.model_dump(mode="json"); raw["ruleset_version"]=SERVICE_RULESET
    raw["input_checksum"]=verified_request_digest({k:v for k,v in raw.items() if k!="input_checksum"})
    manifest=AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw))
    return manifest,request,build_service_capability_report(manifest,request,previous_report=previous)


def test_service_findings_all_have_explicit_rule_kind_mappings():
    from app.report_v22.capability_finding_status import rule_kinds
    report=labelled_service_fixture()[2]
    assert report.schema_version=="2.2.5"
    assert any(f.rule_id.startswith("V22.") for f in report.findings)
    for finding in report.findings:
        assert finding.kind==rule_kinds()[finding.rule_id]
        assert finding.status and finding.coverage


def test_completion_reference_uses_actual_report_version():
    from types import SimpleNamespace
    from datetime import datetime, timezone, timedelta
    from app.api.v2.runtime import state_to_public
    report=labelled_public_fixture()[2]
    now=datetime.now(timezone.utc)
    state=SimpleNamespace(report=report,job_id=report.id,case_id=report.input_manifest.case_id,
        revision=3,run_generation=1,message="Analysis complete.",created_at=now-timedelta(minutes=1),
        deadline_at=now+timedelta(minutes=10),updated_at=now)
    assert state_to_public(state).report_reference.schema_version=="2.2.5"


def contact_report_fixture(case, *, ruleset=PRIOR_PUBLIC_RULESET):
    from unittest.mock import patch
    from app.report_v22.capability import AnalysisInputManifest
    from app.report_v22.capability_public import build_public_capability_report
    from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
    if case == 'rendered':
        from test_p0_04_render_fallback import versioned_report_fixture
        manifest, request, legacy = versioned_report_fixture()
    else:
        from test_v22_capability_public import inputs
        from test_v22_site_gbp_alignment import exact_html, exact_gbp
        from site_business_helpers import source
        html = {'structured':exact_html(), 'mismatch':exact_html(telephone='+1 512 555 0199'), 'text_pattern':'<footer>123 Fixture St</footer>',
                'not_checked':'<div id="app"></div>'}[case]
        with patch('test_v22_capability_public.sample_input', return_value=exact_gbp()):
            manifest, request = inputs(business_source=source(html))
        legacy = build_public_capability_report(manifest, request)
    before = canonical_json_bytes(request)
    raw = manifest.model_dump(mode='json')
    raw['ruleset_version'] = ruleset
    raw['input_checksum'] = verified_request_digest({k:v for k,v in raw.items() if k!='input_checksum'})
    report = build_public_capability_report(AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)), request)
    assert canonical_json_bytes(request) == before
    return report, legacy


@pytest.mark.parametrize('case,field,status,basis,parts,result', [
    ('structured','phone','confirmed','structured',['number'],'match'),
    ('mismatch','phone','confirmed','structured',[],'mismatch'),
    ('text_pattern','address','confirmed','text_pattern',['street'],'match'),
    ('rendered','phone','confirmed','rendered',['phone'],'match'),
    ('not_checked','phone','not_checked','structured',[],'none'),
])
def test_contact_checks_copy_alignment_without_findings_actions_or_history_changes(case, field, status, basis, parts, result):
    from app.jobs_v22.digest import canonical_json_bytes
    report, legacy = contact_report_fixture(case)
    check = next(c for c in report.contact_checks if c.field == field)
    assert (check.status, check.basis, check.compared_components) == (status, basis, parts)
    assert check.result == result
    if case == 'mismatch':
        assert next(f for f in report.findings if f.rule_id == 'v22_public.gbp_phone_alignment').status == 'confirmed'
    if case == 'text_pattern': assert check.omitted_components == ['remaining_address_components']
    assert len(report.contact_checks) == len({c.field for c in report.contact_checks}) == 4
    assert all(set(c.evidence_ids) <= {e.id for e in report.evidence} for c in report.contact_checks)
    if status == 'confirmed': assert check.evidence_ids
    assert canonical_json_bytes(report.model_dump(mode='json')['actions']) == canonical_json_bytes(legacy.model_dump(mode='json')['actions'])
    assert [f.id for f in report.findings] == [f.id for f in legacy.findings]
    assert 'contact_checks' not in legacy.model_dump(mode='json')


@pytest.mark.parametrize('mutation', ['missing','null','duplicate','unknown_evidence','old_version','missing_result','invalid_result'])
def test_contact_check_contract_requires_v8_unique_fields_and_bound_evidence(mutation):
    from app.report_v22.capability import CapabilityReport
    from app.jobs_v22.digest import canonical_json_bytes
    from pydantic import ValidationError
    report, legacy = contact_report_fixture('structured')
    raw = report.model_dump(mode='json')
    if mutation == 'missing_result': raw['contact_checks'][0].pop('result')
    if mutation == 'invalid_result': raw['contact_checks'][0]['result'] = 'confirmed'
    if mutation == 'missing': raw.pop('contact_checks')
    if mutation == 'null': raw['contact_checks'] = None
    if mutation == 'duplicate': raw['contact_checks'][1] = raw['contact_checks'][0]
    if mutation == 'unknown_evidence': raw['contact_checks'][0]['evidence_ids'] = ['unbound-evidence']
    if mutation == 'old_version': raw = {**legacy.model_dump(mode='json'), 'contact_checks':raw['contact_checks']}
    with pytest.raises(ValidationError): CapabilityReport.model_validate_json(canonical_json_bytes(raw))


@pytest.mark.parametrize('state,result', [
    ('exact_match','match'), ('semantic_match','match'), ('partial_match','partial_match'),
    ('mismatch','mismatch'), ('site_missing','none'), ('gbp_missing','none'),
    ('both_missing','none'), ('not_checked','none'), ('not_applicable','none'),
])
def test_contact_result_copies_alignment_state_without_changing_status(state, result):
    from types import SimpleNamespace
    from app.report_v22.capability_contact_checks import contact_checks_from_alignment
    from app.report_v22.site_gbp_alignment_models import SiteGbpFieldResult
    field = SiteGbpFieldResult(field='phone',state=state,
        not_checked_reason='source_missing' if state == 'not_checked' else None)
    checks = contact_checks_from_alignment(SimpleNamespace(fields=[field]),
        SimpleNamespace(evidence_input=SimpleNamespace(sources=[])), {})
    assert checks[0]['result'] == result
    assert checks[0]['status'] == alignment_status(state)
