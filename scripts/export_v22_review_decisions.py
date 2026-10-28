"""Export decision workspace schema and explicitly synthetic fixtures."""
import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.report_v22.review_decision_workspace import DecisionWorkspaceContracts, context
from tests.test_v22_review_decisions import example, request, receipt


def artifacts():
    saved, _ = example()
    saved = saved.model_copy(update={"id": UUID("70000000-0000-4000-8000-000000000001")})
    req = request(saved, id="70000000-0000-4000-8000-000000000099")
    return {"workspace.schema.json": DecisionWorkspaceContracts.model_json_schema(mode="serialization"),
        "fixture.json": dict(kind="synthetic_decision_workspace", context=context(saved).model_dump(mode="json"), request=req, revision=receipt(saved,req))}


if __name__ == "__main__":
    p=argparse.ArgumentParser();p.add_argument("--frontend-dir",type=Path);p.add_argument("--check",action="store_true");a=p.parse_args()
    roots=[ROOT/"contracts/v2.2-review-decisions"]
    if a.frontend_dir:
        if not (a.frontend_dir/"package.json").is_file(): raise SystemExit("Invalid frontend root")
        roots.append(a.frontend_dir/"src/lib/review-decisions/contracts")
    for root in roots:
        for name,value in artifacts().items():
            path=root/name;data=(json.dumps(value,sort_keys=True,indent=2)+"\n").encode()
            if a.check:
                if not path.is_file() or path.read_bytes()!=data: raise SystemExit("Decision workspace contract drift")
            else:
                root.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    print(json.dumps(dict(ok=True,check=a.check,destinations=len(roots))))
