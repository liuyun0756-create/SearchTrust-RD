#!/usr/bin/env python3
"""Separate additive P5 artifacts: no rewriting of historic report contracts."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability_examples import example_report
from app.report_v22.implementation_records import ImplementationRecord


def artifacts():
    report = example_report("first_service", 1)
    delivery = dict(schema_version="v22_delivery_revision_v1", report_id=report["id"],
        user_id=report["input_manifest"]["user_id"], case_id=report["input_manifest"]["case_id"],
        expected_revision=0, source_checksum=verified_request_digest(report), status="approved",
        title=report["title"], summary=report["summary"], client_note=None,
        actions=[{k:v for k,v in a.items() if k not in {"finding_ids", "dependencies"}} for a in report["actions"]])
    source = dict(userId=delivery["user_id"], caseId=delivery["case_id"], reportId=report["id"],
        reportChecksum=delivery["source_checksum"], deliveryRevisionId="b0000000-0000-4000-8000-000000000001",
        deliveryRevision=1, actions=[{k:a[k] for k in ("id", "title", "target")} for a in delivery["actions"]])
    content = dict(schema_version="v22_implementation_record_v1", id="b0000000-0000-4000-8000-000000000002",
        user_id=source["userId"], case_id=source["caseId"], report_id=source["reportId"], report_checksum=source["reportChecksum"],
        delivery_revision_id=source["deliveryRevisionId"], delivery_revision=1, revision=1,
        recorded_at="2026-10-22T08:00:00.000Z", entries=[dict(action_id=source["actions"][0]["id"],
        scope=source["actions"][0]["target"], implemented_on="2026-10-20", timezone="Europe/London", note="Updated approved service details.", responsible=None)])
    record = dict(content, checksum=verified_request_digest(content))
    ImplementationRecord.model_validate_json(json.dumps(record))
    values = {"implementation.schema.json": ImplementationRecord.model_json_schema(mode="serialization"),
        "fixture.json": dict(report=report, delivery=delivery, source=source, history=[record])}
    return {name: (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+"\n").encode() for name,value in values.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    roots = [ROOT/"contracts/v2.2-implementation"]
    if args.frontend_dir:
        if not (args.frontend_dir/"package.json").is_file(): raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir/"src/lib/implementation-records/contracts")
    expected = artifacts()
    for root in roots:
        if args.check:
            if {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()} != expected:
                raise SystemExit("Implementation contract drift")
        else:
            for name, raw in expected.items():
                target = root/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
    print(json.dumps(dict(ok=True, check=args.check, artifacts=len(expected), destinations=len(roots))))
