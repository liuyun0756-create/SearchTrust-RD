"""Formal review report schema and synthetic contract fixture, never live data."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.report_v22.review_report import VerifiedReviewReport,build_verified_review_report
from tests.test_v22_review_generation import generation
from tests.test_v22_review_decisions import zero_evidence


def artifacts():
    return {"report.schema.json":VerifiedReviewReport.model_json_schema(mode="serialization"),
        "fixture.json":dict(kind="synthetic_formal_review_reports",reports=dict(
            baseline=build_verified_review_report(generation()).model_dump(mode="json"),
            zero_actions=build_verified_review_report(generation(saved=zero_evidence(),decisions=[])).model_dump(mode="json")))}


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--frontend-dir",type=Path);parser.add_argument("--check",action="store_true");a=parser.parse_args()
    roots=[ROOT/"contracts/v2.2-review-reports"]
    if a.frontend_dir:
        if not (a.frontend_dir/"package.json").is_file():raise SystemExit("Invalid frontend root")
        roots.append(a.frontend_dir/"src/lib/review-reports/contracts")
    for root in roots:
        for name,value in artifacts().items():
            path=root/name;data=(json.dumps(value,sort_keys=True,indent=2)+"\n").encode()
            if a.check:
                if not path.is_file() or path.read_bytes()!=data:raise SystemExit("Review report contract drift")
            else:root.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    print(json.dumps(dict(ok=True,check=a.check,destinations=len(roots))))
