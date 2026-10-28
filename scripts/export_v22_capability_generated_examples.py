#!/usr/bin/env python3
"""Local QA only: execute real rules over fixed synthetic source fixtures.

Not a production collector, customer result or marketing fixture. Never reads
credentials, calls providers or reserves credits. Outputs are deterministic.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"tests")]
from test_v22_capability_public import inputs as public_inputs
from test_v22_capability_service import inputs as service_inputs
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability_service import build_service_capability_report
from app.report_v22.capability import AnalysisInputManifest
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest


def artifacts():
    values={}
    for count,records,gap in [(0,[{"title":"Plumbing"}],False),(1,[{"status_code":503}],False),
            (2,[{"status_code":503},{"title":"Shared"},{"title":"Shared"}],False),
            (3,[{"status_code":503},{"title":"Shared"},{"title":"Shared"}],True)]:
        manifest,request=public_inputs(records,search_gap=gap)
        values[f"public-{count}.json"]=build_public_capability_report(manifest,request).model_dump(mode="json")
    for name,options in [("service-zero",{"clean":True}),("service-first",{}),
            ("service-after",{"mode":"after_acquisition"}),("service-conflict",{"conflict":True}),
            ("service-measurement",{"not_set":True})]:
        manifest,request,parent=service_inputs(**options)
        values[f"{name}.json"]=build_service_capability_report(manifest,request,previous_report=parent).model_dump(mode="json")
    for name,focus in [("service-target-focus", dict(target_pages=["https://example.test/service"], priority="service_visibility")),
            ("service-other-focus", dict(target_pages=["https://example.test/other"], priority="business_identity")),
            ("service-unstructured-focus", dict(priority="Increase revenue")),
            ("service-limited-capacity", dict(execution_conditions={"capacity":"limited","publishing_access":"needs_approval"})),
            ("service-contact-focus", dict(priority="contact_path"))]:
        manifest,request,_=service_inputs()
        raw=manifest.model_dump(mode="json")
        raw["business"].update(focus)
        raw["input_checksum"]=verified_request_digest({k:v for k,v in raw.items() if k!="input_checksum"})
        manifest=AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw))
        values[f"{name}.json"]=build_service_capability_report(manifest,request).model_dump(mode="json")
    payloads={name:(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode() for name,value in values.items()}
    manifest={"schema_version":"2.2.2","purpose":"Actual rules on synthetic saved sources; not customer results",
        "files":{name:"sha256:"+hashlib.sha256(raw).hexdigest() for name,raw in sorted(payloads.items())}}
    payloads["manifest.json"]=(json.dumps(manifest,sort_keys=True,indent=2)+"\n").encode()
    return payloads


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--frontend-dir",type=Path)
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args()
    destinations=[ROOT/"tests/fixtures/report_v22_capability_generated"]
    if args.frontend_dir:
        if not (args.frontend_dir/"package.json").is_file(): raise SystemExit("Invalid frontend root")
        destinations.append(args.frontend_dir/"src/lib/report-capability/fixtures/generated")
    outputs=artifacts()
    for destination in destinations:
        if args.check:
            current={p.relative_to(destination).as_posix():p.read_bytes() for p in destination.rglob("*") if p.is_file()}
            if current!=outputs: raise SystemExit("Generated report fixture drift")
        else:
            destination.mkdir(parents=True,exist_ok=True)
            for name,content in outputs.items(): (destination/name).write_bytes(content)
    print(json.dumps({"ok":True,"check":args.check,"artifact_count":len(outputs),"destinations":len(destinations)}))


if __name__=="__main__": main()
