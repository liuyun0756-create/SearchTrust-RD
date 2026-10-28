"""Export audit/review contracts and a synthetic, explicitly non-final observation."""
import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.report_v22.ga4_evidence_review import Ga4ReviewContracts, Ga4ReviewView
from app.report_v22.ga4_evidence_audit import audit_ga4
from tests.support.ga4_comparison_fixtures import fixture


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    roots = [ROOT / "contracts/v2.2-ga4-evidence-review"]
    if args.frontend_dir:
        if not (args.frontend_dir / "package.json").is_file(): raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir / "src/lib/ga4-evidence-reviews/contracts")
    value = fixture()
    view = Ga4ReviewView(schema_version="v22_ga4_review_view_v1", case_id=value.plan.case_id,
        report_id=value.plan.report_id, delivery_revision_id=value.plan.delivery_revision_id,
        audit=audit_ga4(UUID(int=20301), value.snapshot), reviews=[], saved=None)
    artifacts = {"review.schema.json": Ga4ReviewContracts.model_json_schema(mode="serialization"),
        "fixture.json": view.model_dump(mode="json")}
    for root in roots:
        for name, value in artifacts.items():
            target = root / name
            raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
            if args.check:
                if not target.is_file() or target.read_bytes() != raw: raise SystemExit("GA4 review contract drift")
            else:
                root.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
    print(json.dumps(dict(ok=True, check=args.check, destinations=len(roots))))
