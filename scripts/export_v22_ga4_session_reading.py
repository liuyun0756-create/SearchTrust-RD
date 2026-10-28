"""Generate the independent session-reading contract and synthetic fixtures."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.report_v22.ga4_session_reading import Ga4SessionReading, read_sessions
from tests.support.ga4_session_fixtures import session_job


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    roots = [ROOT / "contracts/v2.2-ga4-session-reading"]
    if args.frontend_dir:
        if not (args.frontend_dir / "package.json").is_file(): raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir / "src/lib/ga4-session-readings/contracts")
    artifacts = {"reading.schema.json": Ga4SessionReading.model_json_schema(mode="serialization"),
        "fixture.json": read_sessions(session_job()).model_dump(mode="json")}
    for root in roots:
        for name, value in artifacts.items():
            target = root / name; raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
            if args.check:
                if not target.is_file() or target.read_bytes() != raw: raise SystemExit("Session reading contract drift")
            else:
                root.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
    print(json.dumps(dict(ok=True, check=args.check, destinations=len(roots))))
