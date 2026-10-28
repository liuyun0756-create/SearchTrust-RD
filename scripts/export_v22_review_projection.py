"""Export internal review schemas and explicitly synthetic, deterministic examples."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.report_v22.review_evidence_models import ReviewEvidenceBundle
from app.report_v22.review_projection import build_review_projection
from app.report_v22.review_projection_diff import compare_review_projections
from app.report_v22.review_projection_models import ReviewProjection, ReviewDifference
from tests.support.review_projection_fixtures import bundle_raw, add_decision


def artifacts():
    raw = bundle_raw()
    inputs = {"baseline": raw, "decline": bundle_raw(after=75), "no_sources": bundle_raw(sources=False),
        "withheld": bundle_raw(mutate_ga4=lambda s: s["current"]["totals"]["metadata"].update(subject_to_thresholding=True)),
        "zero_baseline": bundle_raw(before=0, after=10)}
    for disposition in ("continue", "adjust", "observe", "defer", "new"):
        inputs[disposition] = add_decision(bundle_raw(), disposition, "new-work" if disposition == "new" else "ac-1")
    return {"bundle.schema.json": ReviewEvidenceBundle.model_json_schema(mode="serialization"),
        "projection.schema.json": ReviewProjection.model_json_schema(mode="serialization"),
        "difference.schema.json": ReviewDifference.model_json_schema(mode="serialization"),
        "fixtures.json": dict(kind="synthetic_contract_examples", bundle=raw,
            projections={k: build_review_projection(v).model_dump(mode="json") for k, v in inputs.items()},
            differences=dict(first=compare_review_projections(raw).model_dump(mode="json"),
                unchanged=compare_review_projections(raw, raw).model_dump(mode="json"),
                changed=compare_review_projections(inputs["decline"], raw).model_dump(mode="json")))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    roots = [ROOT / "contracts/v2.2-review-projection"]
    if args.frontend_dir:
        if not (args.frontend_dir / "package.json").is_file():
            raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir / "src/lib/review-projection/contracts")
    for root in roots:
        for name, value in artifacts().items():
            target = root / name
            raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
            if args.check:
                if not target.is_file() or target.read_bytes() != raw:
                    raise SystemExit("Review projection contract drift")
            else:
                root.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
    print(json.dumps(dict(ok=True, check=args.check, destinations=len(roots))))
