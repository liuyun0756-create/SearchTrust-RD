"""Export exact arithmetic aids; no statistical adequacy thresholds are selected."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.report_v22.metric_reading import MetricReadingGuide, metric_reading
from tests.support.metric_reading_fixtures import examples


def artifacts():
    values={"reading.schema.json":MetricReadingGuide.model_json_schema(mode="serialization"),
        "fixtures.json":[dict(name=name,result=result.model_dump(mode="json"),reading=metric_reading(result).model_dump(mode="json")) for name,result in examples()]}
    return {name:(json.dumps(value,indent=2,sort_keys=True)+"\n").encode() for name,value in values.items()}


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--frontend-dir",type=Path);parser.add_argument("--check",action="store_true")
    args=parser.parse_args();roots=[ROOT/"contracts/v2.2-metric-reading"]
    if args.frontend_dir:
        if not (args.frontend_dir/"package.json").is_file():raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir/"src/lib/comparison-checks/metric-reading-contracts")
    for root in roots:
        for name,raw in artifacts().items():
            target=root/name
            if args.check:
                if not target.is_file() or target.read_bytes()!=raw:raise SystemExit("Metric reading contract drift")
            else:
                root.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    print(json.dumps(dict(ok=True,check=args.check,destinations=len(roots))))
