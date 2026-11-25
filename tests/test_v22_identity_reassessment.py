from uuid import UUID

import pytest

from app.jobs_v22.capability_inputs import generation_recipe
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.public_reassessment import reassess_public_report
from test_p0_08_gold import deny_network
from test_p1_02_customer_gbp_reviews import output_fixture

BASE_SELECTION = {
    "operating_model": "hybrid",
    "branch_ref": "cid:12345",
    "reason": "Confirmed at the saved branch",
    "customers_can_visit": True,
}
FIRST_REVIEW = UUID("c44f9b8d-8563-43ab-ae41-64ce02840d50")
SECOND_REVIEW = UUID("5e3b27a1-309d-4c61-b14c-27d95d52b609")


def review_set():
    bound, report = output_fixture()
    after, updated, diff = reassess_public_report(
        report, bound["request"], BASE_SELECTION,
        request_id=FIRST_REVIEW, evaluated_at="2026-08-30T00:05:00Z",
    )
    return bound, report, after, updated, diff


def test_reassessment_keeps_same_branch_samples_and_report_versions(monkeypatch):
    deny_network(monkeypatch)
    bound, report, after, updated, diff = review_set()
    assert after.schema_version == "2.2.17"
    assert after.input_manifest.ruleset_version == "v22_capability_public_v22"
    assert after.input_manifest.schema_version == "v22_analysis_input_v12"
    assert after.input_manifest.identity_inheritance == report.input_manifest.identity_inheritance
    assert after.input_manifest.snapshots == report.input_manifest.snapshots
    assert after.input_manifest.base_report_ref.report_id == report.id
    assert canonical_json_bytes(updated.evidence_input) == canonical_json_bytes(bound["request"].evidence_input)
    assert after.gbp_comparison == report.gbp_comparison
    assert after.possible_reasons == report.possible_reasons
    assert after.settlement_policy == "no_charge_public_reassessment"
    assert diff["requires_approval"]
    assert generation_recipe(after.input_manifest, updated)[1] == updated


@pytest.mark.parametrize("branch", ["cid:999", "place_id:fixture-customer-place"])
def test_reassessment_cannot_rebind_review_sample_to_another_branch_reference(branch, monkeypatch):
    deny_network(monkeypatch)
    bound, report = output_fixture()
    with pytest.raises(ValueError, match="SAVED_BRANCH_DETAILS_REQUIRED"):
        reassess_public_report(report, bound["request"], {**BASE_SELECTION, "branch_ref": branch},
            request_id=FIRST_REVIEW, evaluated_at="2026-08-30T00:05:00Z")


@pytest.mark.parametrize("mode,visits", [(None, True), ("hybrid", False)])
def test_reassessment_can_withdraw_one_confirmation_and_preserve_the_other(mode, visits, monkeypatch):
    deny_network(monkeypatch)
    _, _, base, request, _ = review_set()
    after, updated, _ = reassess_public_report(base, request,
        {**BASE_SELECTION, "operating_model": mode, "customers_can_visit": visits},
        request_id=SECOND_REVIEW, evaluated_at="2026-08-30T00:10:00Z")
    assert bool(updated.business_identity.operating_model.confirmation_ref) == (mode is not None)
    old_visit = request.business_identity.operating_location.confirmation_ref
    retained = updated.business_identity.operating_location.confirmation_ref
    assert bool(retained) == visits
    if retained:
        assert retained.confirmed_at == old_visit.confirmed_at
        assert retained.selected_branch_ref == old_visit.selected_branch_ref
    assert after.gbp_comparison == base.gbp_comparison
    assert after.settlement_policy == "no_charge_public_reassessment"
    assert canonical_json_bytes(updated.evidence_input) == canonical_json_bytes(request.evidence_input)


def test_reassessment_cannot_make_new_visit_confirmation_without_operating_mode(monkeypatch):
    deny_network(monkeypatch)
    bound, report = output_fixture()
    with pytest.raises(ValueError, match="SAVED_BRANCH_ADDRESS_REQUIRED"):
        reassess_public_report(report, bound["request"], {**BASE_SELECTION, "operating_model": None},
            request_id=FIRST_REVIEW, evaluated_at="2026-08-30T00:05:00Z")
