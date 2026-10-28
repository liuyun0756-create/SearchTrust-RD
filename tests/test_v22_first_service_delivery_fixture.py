"""Keep the browser bridge's synthetic sources valid under the actual rules."""
from datetime import datetime, timezone

import pytest

from support.first_service_delivery_bridge import seed
from app.jobs_v22.capability_inputs import generation_recipe
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.capability_service import build_service_capability_report


@pytest.mark.parametrize("clean", [False, True])
def test_delivery_seed_uses_actual_rules_without_mutating_sources(clean):
    manifest, request = seed(clean)
    before = canonical_json_bytes(request)
    bound, value, recipe = generation_recipe(manifest, request)
    report = build_service_capability_report(bound, value)
    assert report.input_manifest.mode == "first_service"
    assert report.input_manifest.previous_report is None and report.changes == []
    assert len(report.actions) == (0 if clean else 3)
    assert {"gsc", "ga4"} <= {e.source_kind for e in report.evidence}
    assert report.settlement_policy == "one_credit_per_completed_analysis"
    assert canonical_json_bytes(request) == before
    assert recipe["request"]["public_input"]["business_identity"] is not None
    age = datetime.now(timezone.utc) - datetime.fromisoformat(manifest.evaluated_at.replace("Z", "+00:00"))
    assert 0 <= age.total_seconds() < 300
