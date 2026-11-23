"""Saved identity inheritance compares material evidence at the final acquisition boundary."""
from uuid import UUID

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.identity_inheritance import PinnedIdentity, inherit_identity
from app.report_v22.identity_input import prepare_identity_input, source_set_checksum
from app.report_v22.public_reassessment import reassess_public_report
from test_p1_05_reassessment import base, selection
from test_p0_08_gold import deny_network


def saved_confirmation():
    report, original = base()
    manifest, original, _, _ = prepare_identity_input(report.input_manifest, original)
    report = build_public_capability_report(manifest, original)
    _, confirmed, _ = reassess_public_report(report, original,
        {**selection(original, 'hybrid'), 'customers_can_visit': True},
        request_id=UUID(int=576), evaluated_at=manifest.evaluated_at)
    gbp = next(s.payload.record for s in original.evidence_input.sources if s.kind == 'public_gbp')
    saved = confirmed.business_identity
    return manifest, original, dict(id=UUID(int=577), revision=4, identity=saved,
        identity_checksum=request_digest(saved),
        entity_keys={key.kind: key.value for key in gbp.observed_entity_keys},
        gbp_website_url=str(gbp.fields.website_url.value) if gbp.fields.website_url.value else None,
        gbp_phone=gbp.fields.phone.value, phone_matched=True)


@pytest.mark.parametrize('change', ['unchanged', 'branch', 'visit', 'mode'])
def test_inheritance_uses_independent_confirmation_gates(change, monkeypatch):
    deny_network(monkeypatch)
    manifest, request, row = saved_confirmation()
    raw = row['identity'].model_dump(mode='json')
    if change == 'branch':
        raw['selected_branch_ref'] = '9999999'
        raw['operating_model']['confirmation_ref']['selected_branch_ref'] = '9999999'
        raw['operating_location']['confirmation_ref']['selected_branch_ref'] = '9999999'
        row['entity_keys']['cid'] = '9999999'
    if change == 'visit':
        raw['operating_location']['location']['address'] = '999 Different Rd, Austin, TX'
        raw['operating_location']['confirmation_ref']['address'] = '999 Different Rd, Austin, TX'
    if change == 'mode':
        raw['operating_model']['conflicts'] = ['different operating evidence']
    row['identity'] = type(row['identity']).model_validate_json(canonical_json_bytes(raw))
    row['identity_checksum'] = request_digest(row['identity'])
    pin = PinnedIdentity.model_validate(row)
    before = canonical_json_bytes(request)
    derived, current = inherit_identity(manifest, request, pin)
    report = build_public_capability_report(derived, current)
    proof = derived.identity_inheritance
    assert report.schema_version == '2.2.16'
    assert proof.source_identity_version_id == pin.id and proof.source_revision == 4
    assert proof.confirmed_at == pin.identity.operating_model.confirmation_ref.confirmed_at
    assert canonical_json_bytes(current.evidence_input) == canonical_json_bytes(request.evidence_input)
    assert canonical_json_bytes(request) == before
    assert derived.generation_input_checksum == verified_request_digest(current.model_dump(mode='json'))
    assert (proof.branch, proof.operating_model, proof.visits) == {
        'unchanged': ('retained', 'retained', 'retained'),
        'branch': ('needs_confirmation', 'needs_confirmation', 'needs_confirmation'),
        'visit': ('retained', 'retained', 'needs_confirmation'),
        'mode': ('retained', 'needs_confirmation', 'retained'),
    }[change]
    identity = current.business_identity
    if change in {'unchanged', 'visit'}:
        assert identity.operating_model.value == 'hybrid'
        assert identity.operating_model.confirmation_ref.evidence_checksum == source_set_checksum(current)
    else:
        assert identity.operating_model.confirmation_ref is None
    assert (identity.operating_location.role == 'customer_facing') == (change in {'unchanged', 'mode'})
