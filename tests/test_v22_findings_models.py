from uuid import UUID

import pytest
from pydantic import ValidationError

from app.report_v22.findings_models import PublicFindingsInput, PublicFindingsLimits, RuleTarget
from app.report_v22.site_gbp_alignment_models import SiteGbpAlignmentLimits
from app.report_v22.site_business_models import SiteBusinessLimits
from app.report_v22.findings_identity import finding_id
from evidence_helpers import build_input


def test_input_and_limits_are_strict():
    request = PublicFindingsInput(evidence_input=build_input())
    assert request.limits.max_findings == 10_000
    assert request.business_identity is None
    assert request.site_business_limits == SiteBusinessLimits()
    assert request.site_gbp_alignment_limits == SiteGbpAlignmentLimits()
    for changes in ({"max_findings": "1"}, {"max_findings": 10001}, {"unexpected": 1}):
        with pytest.raises(ValidationError):
            PublicFindingsLimits(**changes)


def test_alignment_input_accepts_only_a_strict_confirmed_identity():
    identity = {
        "business_name": "Example Plumbing LLC",
        "site_url": "https://example.test/",
        "normalized_domain": "example.test",
        "operating_model": "hybrid",
        "primary_location": {
            "display_name": "Austin, TX",
            "country_code": "US",
            "latitude": 30.2672,
            "longitude": -97.7431,
        },
    }
    request = PublicFindingsInput(evidence_input=build_input(), business_identity=identity)
    assert request.business_identity.business_name == "Example Plumbing LLC"
    with pytest.raises(ValidationError):
        PublicFindingsInput(evidence_input=build_input(), business_identity={**identity, "unexpected": True})


@pytest.mark.parametrize("changes", [
    {"max_candidates": 5_001},
    {"max_pair_comparisons": 250_001},
    {"max_comparison_age_days": 31},
    {"max_evidence_references": "20"},
    {"unexpected": 1},
])
def test_alignment_limits_are_strict_and_only_downward_configurable(changes):
    with pytest.raises(ValidationError):
        SiteGbpAlignmentLimits(**changes)


def test_identity_is_stable_versioned_and_reference_order_independent():
    target = RuleTarget(kind="site")
    params = dict(case_id=UUID(int=1), rule_id="v22_public.site_http_error", rule_version="1.0.0", target=target,
                  evidence_ids=["ev_bbb", "ev_aaa"], comparator_ids=[])
    identifier = finding_id(**params)
    assert identifier.startswith("fn_") and len(identifier) == 67
    assert identifier == finding_id(**{**params, "evidence_ids": ["ev_aaa", "ev_bbb"]})
    assert identifier != finding_id(**{**params, "rule_version": "1.0.1"})
    assert identifier != finding_id(**{**params, "case_id": UUID(int=2)})
