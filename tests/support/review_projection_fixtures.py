"""Deterministic constructed evidence; no real customer or provider access."""
import json
from pathlib import Path
from uuid import UUID

from app.jobs_v22.digest import verified_request_digest, verified_canonical_json_bytes
from app.report_v22.capability import CapabilityReport, DeliveryRevision
from app.report_v22.comparison import compare_implementation
from app.report_v22.comparison_storage import FrozenComparison, ComparisonSelection
from app.report_v22.ga4_comparison_models import Ga4ExactSnapshotV2, qualify_ga4
from app.report_v22.page_implementation_checks import prepare_page_check, checksum_model
from app.report_v22.page_implementation_models import PageCondition, PageObservation
from app.report_v22.review_evidence_models import RULES, ReviewEvidenceCore
from tests.support.comparison_fixtures import request_raw, parsed, resign_source
from tests.support.ga4_session_fixtures import session_job

FIXTURE = Path(__file__).resolve().parents[2] / "contracts/v2.2-implementation/fixture.json"


def seal(raw):
    raw["evidence_checksum"] = verified_request_digest({k: v for k, v in raw.items() if k in ReviewEvidenceCore.model_fields})
    for d in raw["decisions"]:
        d["evidence_checksum"] = raw["evidence_checksum"]
    raw["checksum"] = verified_request_digest({k: v for k, v in raw.items() if k != "checksum"})
    return raw


def align_gsc(request):
    for name in ("before", "after"):
        s = request[name]
        if s is not None:
            s["external_resource_id"] = "sc-domain:clinic.example.invalid"
            s["normalized_payload"]["resource_id"] = s["external_resource_id"]
            for period in ("previous", "current"):
                for row in s["normalized_payload"][period]["pages"]["rows"]:
                    row["key"] = row["key"].replace("example.test", "clinic.example.invalid")
            resign_source(s)
    return request


def bundle_raw(*, sources=True, before=100, after=125, mutate_ga4=None):
    f = json.loads(FIXTURE.read_text())
    request = parsed(align_gsc(request_raw()))
    record = request.implementation
    report = CapabilityReport.model_validate_json(json.dumps(f["report"]))
    delivery = DeliveryRevision.model_validate_json(json.dumps(f["delivery"]))
    result = compare_implementation(request)
    selection = ComparisonSelection(implementation=result.implementation, action_id=request.action_id, days=request.days,
        scope=request.scope, before_snapshot_id=request.before.snapshot_id, after_snapshot_id=request.after.snapshot_id,
        concurrent_changes=request.concurrent_changes)
    frozen = FrozenComparison(schema_version="v22_frozen_comparison_v1", id=UUID(int=31001), user_id=record.user_id,
        case_id=record.case_id, selection=selection, request=request, result=result)
    plan = prepare_page_check(implementation=record.model_dump(mode="json"), business=report.input_manifest.business,
        action_id=request.action_id, target_url=report.input_manifest.business.site_url.rstrip("/")+"/service",
        conditions=(PageCondition(id="contact", kind="anchor_present", value="contact"),), report=report,
        user_id=record.user_id, case_id=record.case_id, delivery_revision_id=record.delivery_revision_id,
        delivery_revision=record.delivery_revision, delivery_content=delivery)
    page = checksum_model(PageObservation, dict(schema_version="v22_page_implementation_observation_v1", id=str(UUID(int=31002)),
        plan=plan.model_dump(mode="json"), observed_at="2026-10-23T08:00:00Z", status="checked", failure=None,
        failure_stage=None, http_status=200, media_type="text/html", response_bytes=100, content_checksum="sha256:"+"1"*64,
        results=[dict(condition_id="contact", state="observed")],
        limitation="static_response_only_rendering_functionality_and_identity_not_verified", effect_conclusion=None, causal_eligible=False))
    selections = [dict(action_id=a.id, page=None, gsc=None, ga4=None) for a in delivery.actions]
    if sources:
        ga4 = session_job(before, after, mutate_ga4).model_dump(mode="json")
        ga4["plan"]["scope"]["hosts"] = ["clinic.example.invalid"]
        ga4["plan"]["checksum"] = verified_request_digest({k: v for k, v in ga4["plan"].items() if k != "checksum"})
        ga4["collection"]["plan"] = ga4["plan"]
        ga4["collection"]["snapshot"]["scope"]["hosts"] = ["clinic.example.invalid"]
        ga4["collection"]["qualification"] = qualify_ga4(Ga4ExactSnapshotV2.model_validate_json(json.dumps(ga4["collection"]["snapshot"]))).model_dump(mode="json")
        ga4["collection"]["checksum"] = verified_request_digest({k: v for k, v in ga4["collection"].items() if k != "checksum"})
        ga4["connection_id"] = str(UUID(int=31003))
        ga4["selection"]["scope_snapshot_id"] = str(UUID(int=31004))
        selections[0].update(page=dict(current=page.model_dump(mode="json"), previous=None), gsc=frozen.model_dump(mode="json"),
            ga4=ga4)
    return seal(dict(schema_version="v22_review_evidence_bundle_v1", rules_version=RULES, user_id=str(record.user_id),
        case_id=str(record.case_id), evaluated_at="2026-12-16T08:00:00Z", report=report.model_dump(mode="json"),
        report_checksum=verified_request_digest(report.model_dump(mode="json")), delivery_id=str(record.delivery_revision_id),
        delivery_revision=record.delivery_revision, delivery=delivery.model_dump(mode="json"),
        delivery_checksum=verified_request_digest(delivery.model_dump(mode="json")), implementation=record.model_dump(mode="json"),
        selections=selections, decisions=[]))


def add_decision(raw, disposition="continue", action_id="ac-1"):
    d = dict(source="seo_recorded", evidence_checksum=raw["evidence_checksum"], recorded_at="2026-12-16T07:00:00Z",
        action_id=action_id, disposition=disposition, reason="Discuss the checked contact condition.",
        references=[f"implementation:{raw['implementation']['id']}:ac-1"], target=None, work=None,
        completion_statement=None, reconsider_when=None)
    if disposition in {"adjust", "new"}:
        d.update(target="Confirmed service page", work="Review the approved contact instructions.")
    if disposition == "observe":
        d["completion_statement"] = "I have completed the described work."
    if disposition == "defer":
        d["reconsider_when"] = "Once the client approves the service details."
    raw["decisions"].append(d)
    return seal(raw)
