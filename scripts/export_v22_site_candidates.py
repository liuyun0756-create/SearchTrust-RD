#!/usr/bin/env python3
"""Offline release review: actual builders, explicitly synthetic inputs.

Writes validated report objects to stdout for the FE allowlist projection.
Never accepts customer files, credentials, a collector URL or billing input.
This is not a production dataset anonymizer.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from test_v22_capability_public import inputs as public_inputs
from test_v22_capability_service import inputs as service_inputs
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability_service import build_service_capability_report


def candidates():
    manifest, request = public_inputs([{"status_code": 503}])
    prospect = build_public_capability_report(manifest, request)
    manifest, request, parent = service_inputs(mode="after_acquisition", records=[{"status_code": 503}])
    service = build_service_capability_report(manifest, request, previous_report=parent)
    # Verify these are two stages of the same constructed case and same parent.
    assert parent == prospect.model_dump(mode="json")
    manifest, request = public_inputs([{"title": "Plumbing"}])
    diagnosis = build_public_capability_report(manifest, request)
    assert not diagnosis.actions and diagnosis.action_state == "insufficient_basis"
    return {
        "purpose": "release_candidate_synthetic_sources_v1",
        "reports": {"prospect": prospect.model_dump(mode="json"),
                    "verified": service.model_dump(mode="json"),
                    "diagnosis": diagnosis.model_dump(mode="json")},
    }


if __name__ == "__main__":
    print(json.dumps(candidates(), ensure_ascii=False, sort_keys=True))
