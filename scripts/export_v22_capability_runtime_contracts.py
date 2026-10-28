"""Export separate runtime schemas; do not alter the legacy API bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.jobs_v22.capability_runtime_models import CapabilityRequestEnvelope,CapabilityTaskStatusResponse
from app.jobs_v22.capability_dispatch import CapabilityDispatch


def artifacts():
    values={}
    for name,model in [("request",CapabilityRequestEnvelope),("completed",CapabilityTaskStatusResponse),("dispatch",CapabilityDispatch)]:
        schema=model.model_json_schema(mode="serialization")
        schema["$schema"]="https://json-schema.org/draft/2020-12/schema"
        values[name+".schema.json"]=schema
    values["request.json"]=dict(schema_version="v22_capability_request_envelope_v1",capability_request=dict(
        case_id="00000000-0000-0000-0000-000000000111",manifest_id="00000000-0000-0000-0000-000000000222",
        input_checksum="sha256:"+"a"*64,report_type="prospect"))
    values["completed.json"]=dict(schema_version="v22_capability_task_status_v1",job_id="00000000-0000-0000-0000-000000000333",
        revision=4,run_generation=1,status="succeeded",stage="completed",progress=100,message="Analysis complete.",
        report_reference=dict(report_id="00000000-0000-0000-0000-000000000333",case_id="00000000-0000-0000-0000-000000000111",
            schema_version="2.2.2",report_type="prospect",payload_checksum="sha256:"+"b"*64),
        created_at="2026-10-20T08:00:00Z",deadline_at="2026-10-20T08:20:00Z",updated_at="2026-10-20T08:03:00Z")
    CapabilityRequestEnvelope.model_validate_json(json.dumps(values["request.json"]))
    CapabilityTaskStatusResponse.model_validate_json(json.dumps(values["completed.json"]))
    out={name:(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode() for name,value in values.items()}
    out["manifest.json"]=(json.dumps({"version":"v22_capability_runtime_v1","files":{
        name:"sha256:"+hashlib.sha256(raw).hexdigest() for name,raw in sorted(out.items())}},sort_keys=True,indent=2)+"\n").encode()
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument("--frontend-dir",type=Path);p.add_argument("--check",action="store_true");args=p.parse_args()
    roots=[ROOT/"contracts/v2.2-capability-runtime"]
    if args.frontend_dir:
        if not (args.frontend_dir/"package.json").is_file(): raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir/"src/lib/report-capability/runtime-contracts")
    outputs=artifacts()
    for root in roots:
        if args.check:
            if {f.relative_to(root).as_posix():f.read_bytes() for f in root.rglob("*") if f.is_file()}!=outputs: raise SystemExit("Runtime contract drift")
        else:
            root.mkdir(parents=True,exist_ok=True)
            for name,raw in outputs.items(): (root/name).write_bytes(raw)
    print(json.dumps({"ok":True,"check":args.check,"artifact_count":len(outputs),"destinations":len(roots)}))


if __name__=="__main__": main()
