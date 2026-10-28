"""Additive internal exact-GA4 contract. Fixtures are synthetic, not provider proof."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.jobs_v22.digest import verified_request_digest, verified_canonical_json_bytes
from app.report_v22.ga4_comparison_models import Ga4ComparisonCollection, Ga4ExactSnapshot, qualify_ga4
from app.report_v22.ga4_comparison_collection import prepare_ga4_comparison
from tests.support.ga4_comparison_fixtures import fixture, options, record


def artifacts():
    vectors = []
    for name in ("observed_not_final", "missing_dates", "changed_configuration", "waiting"):
        raw = fixture().model_dump(mode="json")
        if name == "missing_dates": raw["snapshot"]["current"]["dates"]["rows"].pop()
        if name == "changed_configuration":
            raw["snapshot"]["configuration_after"]["events"][0]["counting_method"] = "ONCE_PER_SESSION"
        if name == "waiting":
            raw.update(plan=prepare_ga4_comparison(record(), **options(days=90, at="2026-12-01T07:59:59Z")).model_dump(mode="json"),
                state="waiting", snapshot=None, qualification=None)
        else:
            value = Ga4ExactSnapshot.model_validate_json(json.dumps(raw["snapshot"]))
            raw["qualification"] = qualify_ga4(value).model_dump(mode="json")
        raw["checksum"] = verified_request_digest({k: v for k, v in raw.items() if k != "checksum"})
        parsed = Ga4ComparisonCollection.model_validate_json(verified_canonical_json_bytes(raw))
        vectors.append(dict(name=name, collection=parsed.model_dump(mode="json")))
    values = {"collection.schema.json": Ga4ComparisonCollection.model_json_schema(mode="serialization"), "fixtures.json": vectors}
    return {name: (json.dumps(value, indent=2, sort_keys=True)+"\n").encode() for name, value in values.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    roots = [ROOT/"contracts/v2.2-ga4-comparison-collection"]
    if args.frontend_dir:
        if not (args.frontend_dir/"package.json").is_file(): raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir/"src/lib/comparison-checks/ga4-collection-contracts")
    for root in roots:
        for name, data in artifacts().items():
            path = root/name
            if args.check:
                if not path.is_file() or path.read_bytes() != data: raise SystemExit("GA4 comparison contract drift")
            else:
                root.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    print(json.dumps(dict(ok=True, check=args.check, destinations=len(roots))))
