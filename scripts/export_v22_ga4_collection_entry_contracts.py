"""Project collection metadata contracts to the frontend; no source bodies."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.report_v22.ga4_collection_entry import Ga4CollectionContracts

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--frontend-dir",type=Path)
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args()
    roots=[ROOT/"contracts/v2.2-ga4-collection-entry"]
    if args.frontend_dir:
        if not (args.frontend_dir/"package.json").is_file():raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir/"src/lib/ga4-comparison-collections/contracts")
    raw=(json.dumps(Ga4CollectionContracts.model_json_schema(mode="serialization"),indent=2,sort_keys=True)+"\n").encode()
    for root in roots:
        target=root/"entry.schema.json"
        if args.check:
            if not target.is_file() or target.read_bytes()!=raw:raise SystemExit("Collection entry contract drift")
        else:
            root.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    print(json.dumps(dict(ok=True,check=args.check,destinations=len(roots))))
