from uuid import UUID

import pytest
from pydantic import ValidationError

from app.report_v22.findings_models import PublicFindingsInput, PublicFindingsLimits, RuleTarget
from app.report_v22.findings_identity import finding_id
from evidence_helpers import build_input


def test_input_and_limits_are_strict():
    assert PublicFindingsInput(evidence_input=build_input()).limits.max_findings == 10_000
    for changes in ({"max_findings": "1"}, {"max_findings": 10001}, {"unexpected": 1}):
        with pytest.raises(ValidationError):
            PublicFindingsLimits(**changes)


def test_identity_is_stable_versioned_and_reference_order_independent():
    target = RuleTarget(kind="site")
    params = dict(case_id=UUID(int=1), rule_id="v22_public.site_http_error", rule_version="1.0.0", target=target,
                  evidence_ids=["ev_bbb", "ev_aaa"], comparator_ids=[])
    identifier = finding_id(**params)
    assert identifier.startswith("fn_") and len(identifier) == 67
    assert identifier == finding_id(**{**params, "evidence_ids": ["ev_aaa", "ev_bbb"]})
    assert identifier != finding_id(**{**params, "rule_version": "1.0.1"})
    assert identifier != finding_id(**{**params, "case_id": UUID(int=2)})
