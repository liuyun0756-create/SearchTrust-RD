import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import CapabilityReport, DeliveryRevision
from app.report_v22.capability_examples import example_report, invalid_examples, microsecond_example


@pytest.mark.parametrize("mode", ["acquisition","first_service","after_acquisition","review"])
@pytest.mark.parametrize("count", [0,1,2,3])
def test_all_modes_and_action_counts_are_valid(mode,count):
    report=CapabilityReport.model_validate_json(json.dumps(example_report(mode,count)))
    assert len(report.actions)==count
    assert report.settlement_policy=="one_credit_per_completed_analysis"


@pytest.mark.parametrize("case", invalid_examples(), ids=lambda item:item["id"])
def test_invalid_contracts_are_rejected(case):
    with pytest.raises(ValidationError):
        CapabilityReport.model_validate_json(json.dumps(case["report"]))


def revision(report,count=1):
    raw=report.model_dump(mode="json")
    return dict(schema_version="v22_delivery_revision_v1",report_id=raw["id"],
        user_id=raw["input_manifest"]["user_id"],case_id=raw["input_manifest"]["case_id"],
        expected_revision=0,source_checksum=verified_request_digest(raw),status="approved",
        title=raw["title"],summary=raw["summary"],client_note=None,
        actions=[{k:v for k,v in a.items() if k not in {"finding_ids","dependencies"}} for a in raw["actions"]])


@pytest.mark.parametrize("count",[0,1,2,3])
def test_unchanged_approval_is_valid_and_evidence_is_not_editable(count):
    report=CapabilityReport.model_validate_json(json.dumps(example_report("first_service",count)))
    payload=revision(report)
    assert DeliveryRevision.model_validate_json(json.dumps(payload)).validate_report(report)
    payload["evidence"]=deepcopy(report.model_dump(mode="json")["evidence"])
    with pytest.raises(ValidationError):
        DeliveryRevision.model_validate_json(json.dumps(payload))


def test_revision_cannot_change_source_owner_or_action_id():
    report=CapabilityReport.model_validate_json(json.dumps(example_report()))
    for key,value in [("case_id","00000000-0000-0000-0000-000000000099"),
                      ("source_checksum","sha256:"+"0"*64)]:
        payload=revision(report);payload[key]=value
        with pytest.raises(ValueError):
            DeliveryRevision.model_validate_json(json.dumps(payload)).validate_report(report)
    payload=revision(report);payload["actions"][0]["id"]="invented"
    with pytest.raises(ValueError):
        DeliveryRevision.model_validate_json(json.dumps(payload)).validate_report(report)


@pytest.mark.parametrize("field,value", [("evidence_ids",["invented"]), ("readiness","ready_to_use"), ("confirmation_items",[]), ("target","another page"), ("id","invented")])
def test_material_edit_cannot_rewrite_provenance_or_confirmation(field,value):
    report=CapabilityReport.model_validate_json(json.dumps(example_report()))
    payload=revision(report)
    payload["actions"][0]["materials"][0][field]=value
    with pytest.raises(ValueError):
        DeliveryRevision.model_validate_json(json.dumps(payload)).validate_report(report)


def test_material_prose_can_be_edited_without_changing_original_evidence():
    report=CapabilityReport.model_validate_json(json.dumps(example_report()))
    before=report.model_dump(mode="json")
    payload=revision(report)
    payload["summary"]="SEO reviewed the intended contact route with the client."
    payload["client_note"]="Business details supplied by the SEO; not collected evidence."
    payload["actions"][0]["materials"][0]["body"]="Confirm the contact route, then use the approved contact wording."
    assert DeliveryRevision.model_validate_json(json.dumps(payload)).validate_report(report)
    assert report.model_dump(mode="json")==before


def test_input_hash_preserves_timestamp_precision_and_original_evidence_bytes():
    report=microsecond_example()
    assert report["evidence"][0]["value"]==" Contact us "
    assert report["input_manifest"]["snapshots"][0]["fetched_at"].endswith(".123456Z")
    assert CapabilityReport.model_validate_json(json.dumps(report))


def test_history_badges_require_the_actual_previous_payload_and_changed_finding():
    previous=CapabilityReport.model_validate_json(json.dumps(example_report()))
    raw=example_report("after_acquisition")
    raw["id"]="00000000-0000-0000-0000-000000000088"
    m=raw["input_manifest"];m["job_id"]=raw["id"]
    m["previous_report"]["report_id"]=str(previous.id)
    m["previous_report"]["payload_checksum"]=verified_request_digest(previous.model_dump(mode="json"))
    raw["findings"][0]["judgment"]="The enquiry route was clarified in the new evidence review."
    raw["changes"]=[dict(finding_id="fn-contact",
        previous_fingerprint=verified_request_digest(previous.findings[0].model_dump(mode="json")),
        current_fingerprint=verified_request_digest(raw["findings"][0]),reason="The stated judgment changed.")]
    m["input_checksum"]=verified_request_digest({k:v for k,v in m.items() if k!="input_checksum"})
    current=CapabilityReport.model_validate_json(json.dumps(raw))
    assert current.validate_previous_report(previous)
    previous.summary="Different prior payload"
    with pytest.raises(ValueError): current.validate_previous_report(previous)


def test_exported_capability_artifacts_match_the_current_models():
    from scripts.export_v22_capability_contracts import artifacts
    root=Path(__file__).resolve().parents[1]/"contracts/v2.2-capability"
    expected=artifacts()
    assert {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}==expected


def test_already_parsed_models_do_not_skip_nested_input_revalidation():
    report=CapabilityReport.model_validate_json(json.dumps(example_report()))
    payload=DeliveryRevision.model_validate_json(json.dumps(revision(report)))
    report.input_manifest.ruleset_version="mutated-after-validation"
    with pytest.raises(ValidationError): CapabilityReport.model_validate(report)
    with pytest.raises(ValidationError): payload.validate_report(report)
