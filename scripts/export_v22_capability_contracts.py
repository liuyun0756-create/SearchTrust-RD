#!/usr/bin/env python3
"""Additive P1 artifacts; never replaces the existing v2.2 contract directory."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.report_v22.capability import CapabilityContracts
from app.report_v22.capability_examples import example_report, invalid_examples, microsecond_example


def artifacts():
    schema=CapabilityContracts.model_json_schema(mode="serialization")
    def canonical_identifiers(value):
        if isinstance(value,dict):
            if value.get("format")=="uuid": value["pattern"]=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
            for child in list(value.values()): canonical_identifiers(child)
        elif isinstance(value,list):
            for child in value: canonical_identifiers(child)
    canonical_identifiers(schema)
    schema["$schema"]="https://json-schema.org/draft/2020-12/schema"
    schema["title"]="SearchTrust capability contracts 2.2.2"
    values={"capability.schema.json":schema,"fixtures/rejected.json":invalid_examples(),"fixtures/microseconds.json":microsecond_example()}
    for mode in ("acquisition","first_service","after_acquisition","review"):
        for count in range(4): values[f"fixtures/{mode}-{count}.json"]=example_report(mode,count)
    payloads={name:(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+"\n").encode() for name,value in values.items()}
    manifest={"contract_version":"2.2.2","files":{name:"sha256:"+hashlib.sha256(raw).hexdigest() for name,raw in sorted(payloads.items())}}
    payloads["manifest.json"]=(json.dumps(manifest,indent=2,sort_keys=True)+"\n").encode()
    return payloads


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--frontend-dir",type=Path)
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args()
    roots=[ROOT/"contracts/v2.2-capability"]
    if args.frontend_dir:
        if not (args.frontend_dir/"package.json").is_file(): raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir/"src/lib/report-capability/contracts")
    expected=artifacts()
    for root in roots:
        if args.check:
            existing={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}
            if existing != expected: raise SystemExit("Capability contract drift")
        else:
            for name,raw in expected.items():
                target=root/name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(raw)
    print(json.dumps({"ok":True,"check":args.check,"artifact_count":len(expected),"destinations":len(roots)}))


if __name__=="__main__": main()
