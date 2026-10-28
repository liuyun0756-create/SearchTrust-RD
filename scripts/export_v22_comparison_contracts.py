#!/usr/bin/env python3
"""Additive P5 descriptive contract and deterministic synthetic test vectors."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.comparison import compare_implementation
from app.report_v22.comparison_models import ComparisonContracts
from app.report_v22.comparison_windows import plan_windows
from app.report_v22.implementation_records import ImplementationEntry
from tests.support.comparison_fixtures import ga4_request_raw, parsed, request_raw, resign_source


def artifacts():
    fixtures = []
    for name in ("positive", "negative", "unchanged", "zero_baseline", "missing_date", "page_unaligned", "source_changed", "waiting", "ga4_all", "ga4_organic"):
        raw = ga4_request_raw() if name.startswith("ga4") else request_raw()
        if name in {"negative", "unchanged"}:
            clicks, impressions = (8, 160) if name == "negative" else (10, 200)
            for period in ("previous", "current"):
                for row in raw["after"]["normalized_payload"][period]["dates"]["rows"]:
                    if row["key"] >= "2026-09-02": row.update(clicks=clicks, impressions=impressions, ctr=clicks/impressions)
            resign_source(raw["after"])
        if name == "zero_baseline":
            for period in ("previous", "current"):
                for row in raw["before"]["normalized_payload"][period]["dates"]["rows"]:
                    if "2026-08-04" <= row["key"] <= "2026-08-31": row.update(clicks=0, ctr=0.)
            resign_source(raw["before"])
        if name == "missing_date":
            rows = raw["after"]["normalized_payload"]["current"]["dates"]["rows"]
            rows[:] = [r for r in rows if r["key"] != "2026-09-10"]; resign_source(raw["after"])
        if name == "page_unaligned": raw["scope"].update(dimension="page", key="https://example.test/service")
        if name == "source_changed": raw["after"]["binding_id"] = "00000000-0000-4000-8000-000000000001"
        if name == "waiting":
            raw["implementation"]["entries"][0]["implemented_on"] = "2026-10-20"
            raw["implementation"]["checksum"] = verified_request_digest({k:v for k,v in raw["implementation"].items() if k != "checksum"})
        if name == "ga4_organic": raw["scope"]["channel"] = "organic_search"
        request = parsed(raw)
        fixtures.append(dict(name=name, request=request.model_dump(mode="json"), result=compare_implementation(request).model_dump(mode="json")))
    windows = []
    for day, zone in (("2026-09-01", "America/Los_Angeles"), ("2026-03-08", "America/Los_Angeles"), ("2026-11-01", "America/Los_Angeles"),
            ("2024-02-29", "UTC"), ("2026-01-01", "Asia/Shanghai")):
        entry = ImplementationEntry(action_id="window-example", scope="Service page", implemented_on=day, timezone=zone, note=None, responsible=None)
        for days in (7, 14, 28, 90):
            windows.append(dict(entry=entry.model_dump(mode="json"), windows=plan_windows(entry, zone, days).model_dump(mode="json")))
    values = {"comparison.schema.json": ComparisonContracts.model_json_schema(mode="serialization"), "fixtures.json": fixtures, "windows.json": windows}
    return {name: (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+"\n").encode() for name,value in values.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    roots = [ROOT/"contracts/v2.2-comparison"]
    if args.frontend_dir:
        if not (args.frontend_dir/"package.json").is_file(): raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir/"src/lib/implementation-records/comparison-contracts")
    expected = artifacts()
    for root in roots:
        if args.check:
            if {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()} != expected:
                raise SystemExit("Comparison contract drift")
        else:
            for name, raw in expected.items():
                target = root/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
    print(json.dumps(dict(ok=True, check=args.check, artifacts=len(expected), destinations=len(roots))))
