"""Optional coordinates must not turn the same place into a different identity."""
import json
from pathlib import Path
from uuid import uuid4

from pydantic import HttpUrl, ValidationError
import pytest

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.evidence import build_evidence_index
from app.report_v22.findings_errors import FindingsError
from app.report_v22.models import ReportV22, TargetMarket, same_target_market_identity
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_selection import select_site_business_candidates
from app.report_v22.site_gbp_alignment import build_site_gbp_alignment
from test_v22_site_gbp_alignment import exact_gbp, exact_html, identity
from public_gbp_helpers import evidence_input
from site_business_helpers import request

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = json.loads((ROOT/'tests/fixtures/v22_market_identity/e31facd0-recipe-fragment.json').read_text())
RECIPE_REQUEST = FRAGMENT['recipe']['request']
LEFT = RECIPE_REQUEST['business_identity']['primary_location']
RIGHT = RECIPE_REQUEST['evidence_input']['context']['target_market']
MISMATCHES = [('country_code', 'CA'), ('region', 'TX'), ('city', 'Clovis'), ('postal_code', '93721'), ('display_name', 'Clovis, CA, US')]


def market(raw):
    return TargetMarket.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize('left,right', [(LEFT, RIGHT), (RIGHT, LEFT), (LEFT, {**LEFT, 'latitude': 30.0, 'longitude': -97.0})])
def test_identity_excludes_coordinates_without_changing_equality_or_digests(left, right):
    a, b = market(left), market(right)
    before = canonical_json_bytes(a), canonical_json_bytes(b)
    assert a != b  # Do not redefine model equality or frozen/cache identities.
    assert same_target_market_identity(a, b)
    assert (canonical_json_bytes(a), canonical_json_bytes(b)) == before


@pytest.mark.parametrize('field,value', MISMATCHES)
def test_every_actual_identity_field_stays_strict(field, value):
    assert not same_target_market_identity(market(LEFT), market({**RIGHT, field: value}))


def alignment_args():
    site_request = request(exact_html())
    inputs = evidence_input(exact_gbp(), site_request.source)
    inputs.context.target_market = market(RIGHT)
    confirmed = identity(inputs)
    confirmed.primary_location = market(LEFT)
    facts = build_site_business_facts(site_request)
    return dict(context=inputs.context, identity=confirmed, facts=facts,
        selection=select_site_business_candidates(facts, confirmed),
        site_source=site_request.source,
        public_source=next(s for s in inputs.sources if s.kind == 'public_gbp'),
        evidence=build_evidence_index(inputs))


def test_e31facd0_v1_v12_recipe_fragment_passes_actual_alignment_builder():
    assert FRAGMENT['manifest'] == {'schema_version': 'v22_analysis_input_v1', 'ruleset_version': 'v22_capability_public_v12'}
    assert FRAGMENT['recipe']['schema_version'] == 'v22_generation_recipe_v1'
    args = alignment_args()
    before = canonical_json_bytes(args['identity']), canonical_json_bytes(args['context'])
    result = build_site_gbp_alignment(**args)
    assert result.alignment.fields
    assert (canonical_json_bytes(args['identity']), canonical_json_bytes(args['context'])) == before


@pytest.mark.parametrize('field,value', MISMATCHES)
def test_alignment_still_reports_binding_invalid_for_real_identity_mismatch(field, value):
    args = alignment_args()
    args['identity'].primary_location = market({**LEFT, field: value})
    with pytest.raises(FindingsError) as caught:
        build_site_gbp_alignment(**args)
    assert caught.value.error_code == 'V22_FINDINGS_BINDING_INVALID'


@pytest.mark.parametrize('mutation,expected', [
    ('site_url', 'BINDING_INVALID'), ('domain', 'BINDING_INVALID'), ('case_id', 'BINDING_INVALID'),
    ('facts_site_url', 'BINDING_INVALID'), ('public_gbp_url', 'BINDING_INVALID'),
    ('site_checksum', 'CHECKSUM_MISMATCH'), ('gbp_checksum', 'CHECKSUM_MISMATCH'),
])
def test_coordinate_tolerance_does_not_relax_other_identity_or_checksum_guards(mutation, expected):
    args = alignment_args()
    if mutation == 'site_url':
        args['identity'].site_url = HttpUrl('https://other.example/')
    elif mutation == 'domain':
        args['identity'].normalized_domain = 'other.example'
    elif mutation == 'case_id':
        args['facts'].case_id = uuid4()
    elif mutation == 'facts_site_url':
        args['facts'].site_url = HttpUrl('https://other.example/')
    elif mutation == 'public_gbp_url':
        args['identity'].public_gbp_url = HttpUrl('https://www.google.com/maps?cid=999')
    elif mutation == 'site_checksum':
        args['facts'].source_payload_checksum = 'sha256:' + '0'*64
    else:
        args['public_source'].payload.subject_reference_checksum = 'sha256:' + '0'*64
    with pytest.raises(FindingsError) as caught:
        build_site_gbp_alignment(**args)
    assert caught.value.error_code == 'V22_FINDINGS_' + expected


@pytest.mark.parametrize('missing_side', ['market_snapshot', 'case_context'])
def test_legacy_report_accepts_optional_coordinate_asymmetry(missing_side):
    raw = json.loads((ROOT/'contracts/v2.2/fixtures/prospect.json').read_text())
    for section in ('market_snapshot', 'case_context'):
        raw[section]['target_market'].update(latitude=36.8151976, longitude=-119.8921305)
    raw[missing_side]['target_market'].update(latitude=None, longitude=None)
    report = ReportV22.model_validate_json(json.dumps(raw))
    assert same_target_market_identity(report.market_snapshot.target_market, report.case_context.target_market)


@pytest.mark.parametrize('field,value', [('city', 'Different city'), ('postal_code', 'Different postcode')])
def test_legacy_report_rejects_real_market_identity_mismatch(field, value):
    raw = json.loads((ROOT/'contracts/v2.2/fixtures/prospect.json').read_text())
    raw['market_snapshot']['target_market'][field] = value
    with pytest.raises(ValidationError, match='market snapshot target must match case context target'):
        ReportV22.model_validate_json(json.dumps(raw))


def identity_v2_pair():
    from test_p1_05_identity import bound
    from app.report_v22.identity_input import prepare_identity_input
    m, q = bound()
    point = q.evidence_input.context.target_market
    q.evidence_input.context.target_market = point.model_copy(update={'latitude': None, 'longitude': None})
    m, q, _, _ = prepare_identity_input(m, q)
    m.target_market = point.model_copy()
    m.input_checksum = verified_request_digest(m.model_dump(mode='json', exclude={'input_checksum'}))
    return m, q


def test_v13_manifest_context_accepts_coordinate_asymmetry_with_intact_checksums():
    from app.report_v22.capability_public import build_public_capability_report
    m, q = identity_v2_pair()
    report = build_public_capability_report(m, q)
    assert report.input_manifest == m
    assert report.input_manifest.ruleset_version == 'v22_capability_public_v13'


@pytest.mark.parametrize('field,value', [('city', 'Different city'), ('postal_code', 'Different postcode')])
def test_v13_manifest_context_rejects_real_identity_mismatch(field, value):
    from app.report_v22.capability_public import build_public_capability_report, CapabilityPublicError
    m, q = identity_v2_pair()
    m.target_market = m.target_market.model_copy(update={field: value})
    m.input_checksum = verified_request_digest(m.model_dump(mode='json', exclude={'input_checksum'}))
    with pytest.raises(CapabilityPublicError):
        build_public_capability_report(m, q)


def test_v13_identity_checksum_guard_remains_exact():
    from app.report_v22.capability_public import build_public_capability_report, CapabilityPublicError
    m, q = identity_v2_pair()
    m.identity_checksum = 'sha256:' + '0'*64
    m.input_checksum = verified_request_digest(m.model_dump(mode='json', exclude={'input_checksum'}))
    with pytest.raises(CapabilityPublicError):
        build_public_capability_report(m, q)


def test_v12_full_report_accepts_coordinate_presence_difference_with_exact_source_checksums():
    from test_p1_05_identity import bound
    from app.report_v22.capability_public import build_public_capability_report
    m, q = bound()
    context = q.evidence_input.context
    original_point = context.target_market
    context.target_market = original_point.model_copy(update={'latitude': None, 'longitude': None})
    source_bytes = [(s.kind, canonical_json_bytes(s.payload), s.binding.payload_checksum) for s in q.evidence_input.sources]
    q.business_identity.primary_location = original_point.model_copy()
    m.generation_input_checksum = verified_request_digest(q.model_dump(mode='json'))
    m.input_checksum = verified_request_digest(m.model_dump(mode='json', exclude={'input_checksum'}))
    report = build_public_capability_report(m, q)
    assert report.input_manifest == m
    assert m.schema_version == 'v22_analysis_input_v1' and m.ruleset_version == 'v22_capability_public_v12'
    assert [(s.kind, canonical_json_bytes(s.payload), s.binding.payload_checksum) for s in q.evidence_input.sources] == source_bytes
