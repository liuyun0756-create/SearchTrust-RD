"""Generate bounded owner-workspace contracts; fixtures are explicitly synthetic."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.report_v22.review_freeze_storage import ReviewFreezeRepository
from app.report_v22.review_workspace import view
from app.report_v22.review_workspace_models import WorkspaceContracts
from app.jobs_v22.digest import canonical_json_bytes
from tests.test_v22_review_freeze import fixture


def artifacts():
    _, _, raw, ids = fixture()
    raw["id"] = "70000000-0000-4000-8000-000000000001"
    ids["review_id"] = raw["id"]
    saved = ReviewFreezeRepository.checked(canonical_json_bytes(raw), **ids)
    return {"workspace.schema.json": WorkspaceContracts.model_json_schema(mode="serialization"),
        "fixture.json": dict(kind="synthetic_workspace_example", view=view(saved.bundle,saved.selection,"saved",saved=saved).model_dump(mode="json"))}


if __name__ == "__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--frontend-dir",type=Path);parser.add_argument("--check",action="store_true")
    args=parser.parse_args(); roots=[ROOT/"contracts/v2.2-review-workspace"]
    if args.frontend_dir:
        if not (args.frontend_dir/"package.json").is_file(): raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir/"src/lib/review-workspace/contracts")
    for root in roots:
        for name,value in artifacts().items():
            target=root/name;data=(json.dumps(value,sort_keys=True,indent=2)+"\n").encode()
            if args.check:
                if not target.is_file() or target.read_bytes()!=data: raise SystemExit("Workspace contract drift")
            else:
                root.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    print(json.dumps(dict(ok=True,check=args.check,destinations=len(roots))))
