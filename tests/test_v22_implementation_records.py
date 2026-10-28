from copy import deepcopy
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import CapabilityReport, DeliveryRevision
from app.report_v22.implementation_records import ImplementationRecord, implementation_reference, parse_implementation_record

FIXTURE = json.loads((Path(__file__).resolve().parents[1]/"contracts/v2.2-implementation/fixture.json").read_text())


def context():
    f = deepcopy(FIXTURE)
    source = f["source"]
    report = CapabilityReport.model_validate_json(json.dumps(f["report"]))
    delivery = DeliveryRevision.model_validate_json(json.dumps(f["delivery"]))
    args = dict(report=report, user_id=UUID(source["userId"]), case_id=UUID(source["caseId"]),
        delivery_revision_id=UUID(source["deliveryRevisionId"]), delivery_revision=source["deliveryRevision"], delivery_content=delivery)
    return f["history"][0], args


def resign(record):
    record["checksum"] = verified_request_digest({k:v for k,v in record.items() if k != "checksum"})
    return record


def test_exact_approved_action_and_free_record_reference():
    value, args = context()
    record = parse_implementation_record(value, **args)
    ref = implementation_reference(record)
    assert str(ref.id) == value["id"] and ref.revision == 1 and ref.checksum == value["checksum"]
    assert record.entries[0].implemented_on == "2026-10-20"
    # Date corrections create a different revision/checksum, not modified history.
    changed = deepcopy(value)
    changed["revision"] = 2
    changed["entries"][0]["implemented_on"] = "2026-10-19"
    assert parse_implementation_record(resign(changed), **args).checksum != record.checksum
    assert value["entries"][0]["implemented_on"] == "2026-10-20"


@pytest.mark.parametrize("key", ["user_id", "case_id", "report_id", "delivery_revision_id", "delivery_revision", "report_checksum"])
def test_does_not_rebind_owned_records_to_another_source(key):
    value, args = context()
    value[key] = 2 if key == "delivery_revision" else "sha256:"+"0"*64 if key == "report_checksum" else str(uuid4())
    with pytest.raises(ValueError): parse_implementation_record(resign(value), **args)


@pytest.mark.parametrize("change", [
    {"implemented_on": "2026-02-30"}, {"implemented_on": "2026-99-99"}, {"implemented_on": "2026-10-23"},
    {"implemented_on": "0000-01-01"}, {"timezone": "not/a/zone"}, {"timezone": "PST"}, {"note": False},
    {"scope": " "}, {"scope": "x\x00y"}, {"action_id": "foreign-action"}, {"page_verified": True},
])
def test_rejects_invalid_dates_zones_and_inferred_verification(change):
    value, args = context()
    value["entries"][0].update(change)
    with pytest.raises(ValueError): parse_implementation_record(resign(value), **args)


def test_local_date_and_duplicate_or_empty_corrections():
    value, args = context()
    value["recorded_at"] = "2026-10-22T00:30:00Z"
    value["entries"][0].update(implemented_on="2026-10-22", timezone="America/Los_Angeles")
    with pytest.raises(ValueError): parse_implementation_record(resign(value), **args)
    value["entries"][0]["timezone"] = "Asia/Shanghai"
    assert parse_implementation_record(resign(value), **args)
    value["entries"] *= 2
    with pytest.raises(ValueError): parse_implementation_record(resign(value), **args)
    value["entries"] = []
    assert parse_implementation_record(resign(value), **args).entries == []


def test_tampering_strictness_and_draft_are_not_accepted():
    value, args = context()
    value["entries"][0]["scope"] = "Unhashed edit"
    with pytest.raises(ValueError): parse_implementation_record(value, **args)
    resign(value)
    draft = args["delivery_content"].model_copy(update={"status": "draft"})
    with pytest.raises(ValueError): parse_implementation_record(value, **dict(args, delivery_content=draft))
    value["revision"] = True
    with pytest.raises(ValidationError): ImplementationRecord.model_validate_json(json.dumps(resign(value)))


def test_recording_must_not_predate_the_report_and_exact_case_is_required():
    value, args = context()
    value["entries"] = []
    value["recorded_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ValueError): parse_implementation_record(resign(value), **args)
    value, args = context()
    with pytest.raises(ValueError): parse_implementation_record(value, **dict(args, user_id=uuid4()))
