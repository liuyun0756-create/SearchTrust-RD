"""Audit regression tests only; historical production rules are intentionally unchanged."""
import ast
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from app.jobs_v22.capability_acquisition_input import build_acquisition_input
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.verified_report_pipeline import VerifiedReportPipeline
from app.report_v22.capability_public import RULESET, build_public_capability_report
from app.report_v22.findings import build_public_findings
from test_v22_capability_acquisition_preparation import inputs as acquisition_inputs
from test_v22_capability_public import inputs
from verified_pipeline_helpers import frozen_public_fixture


def test_new_acquisition_manifest_uses_current_ruleset_and_executes_reviewed_findings():
    args, _, _ = acquisition_inputs()
    manifest, request = build_acquisition_input(**args)
    from app.report_v22.capability_finding_status import PUBLIC_RULESET
    assert manifest.ruleset_version == PUBLIC_RULESET == "v22_capability_public_v10"
    with patch("app.report_v22.capability_public.build_public_findings", wraps=build_public_findings) as build:
        report = build_public_capability_report(manifest, request)
    assert build.call_args.kwargs == {"text_patterns": True, "reviewed_evidence": True, "rendered_pages": False, "resource_limits": True, "sampling_quality": True}
    assert report.input_manifest == manifest


@pytest.mark.parametrize("version,reviewed", [("v22_capability_public_v3", False),
    ("v22_capability_public_v4", True), ("v22_capability_public_v5", True)])
def test_frozen_versions_keep_their_original_missing_phone_semantics(version, reviewed):
    from site_business_helpers import business_html, source
    manifest, request = inputs(business_source=source(business_html(telephone=None)))
    raw = manifest.model_dump(mode="json")
    raw["ruleset_version"] = version
    raw["input_checksum"] = verified_request_digest({k: v for k, v in raw.items() if k != "input_checksum"})
    before = request.model_dump(mode="json")
    with patch("app.report_v22.capability_public.build_public_findings", wraps=build_public_findings) as build:
        report = build_public_capability_report(raw, request)
    assert build.call_args.kwargs == {"reviewed_evidence": reviewed}
    assert any(f.rule_id.endswith("phone_alignment") for f in report.findings) is (not reviewed)
    assert request.model_dump(mode="json") == before


def test_verified_parent_call_only_validates_and_keeps_the_historical_parent_unchanged():
    payload, request, _, _ = frozen_public_fixture()
    from app.report_v22.models import ReportV22
    parent = ReportV22.model_validate_json(canonical_json_bytes(payload["parent_report"]))
    before = parent.model_dump(mode="json")
    with patch("app.jobs_v22.verified_report_pipeline.build_public_findings", wraps=build_public_findings) as build:
        assert VerifiedReportPipeline._validate_parent_public(parent, request) is None
    assert build.call_args.kwargs == {}  # Historical default is still False.
    assert parent.model_dump(mode="json") == before


def test_verified_pipeline_keeps_legacy_public_findings_in_its_delivered_report():
    import fakeredis.aioredis
    from app.jobs_v22.checkpoints import JobCheckpoints
    from verified_pipeline_helpers import JOB_ID, VERIFIED_AT, resolve_fixture

    async def run():
        resolved, request = await resolve_fixture()
        before = canonical_json_bytes(resolved.parent_report)
        redis = fakeredis.aioredis.FakeRedis()
        try:
            report = await VerifiedReportPipeline(clock=lambda: VERIFIED_AT).build(
                job_id=JOB_ID, request=request, resolved_input=resolved,
                checkpoints=JobCheckpoints(redis, prefix="test:p0-02", ttl_seconds=600))
            delivered = {f.finding_id: f for f in report.findings}
            assert resolved.parent_report.findings
            assert all(delivered[f.finding_id] == f for f in resolved.parent_report.findings)
            assert canonical_json_bytes(resolved.parent_report) == before
        finally:
            await redis.aclose()
    asyncio.run(run())


def test_audit_covers_every_application_import_of_the_public_findings_builder():
    root = Path(__file__).resolve().parents[1]
    callers = set()
    for path in (root / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(isinstance(node, ast.ImportFrom) and node.module == "app.report_v22.findings"
               and any(alias.name == "build_public_findings" for alias in node.names) for node in ast.walk(tree)):
            callers.add(path.relative_to(root).as_posix())
    assert callers == {
        "app/report_v22/capability_public.py", "app/report_v22/verified_reprioritization.py",
        "app/jobs_v22/public_findings_stage.py", "app/jobs_v22/verified_report_pipeline.py",
    }
