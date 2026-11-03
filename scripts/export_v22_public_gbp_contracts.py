"""P0-05 additive snapshot contract, with V1/V2 replay and report fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from pydantic import TypeAdapter
from app.report_v22.public_gbp_models import CustomerPublicGbpSnapshotValue
from test_p0_05_public_gbp_fields import report_fixture


def artifacts():
    schema = TypeAdapter(CustomerPublicGbpSnapshotValue).json_schema(mode='serialization')
    schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
    schema['title'] = 'CustomerPublicGbpSnapshots'
    values = {'snapshot.schema.json': schema}
    for extended in (False, True):
        _, value, report = report_fixture(extended)
        source = next(s for s in value.evidence_input.sources if s.kind == 'public_gbp')
        version = 'v2' if extended else 'v1'
        values[f'{version}-snapshot.json'] = source.payload.model_dump(mode='json')
        values[f'{version}-report.json'] = report.model_dump(mode='json')
    encoded = {name: (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+'\n').encode() for name,value in values.items()}
    manifest = {'version': 'customer_public_gbp_snapshot_v2', 'files': {name: 'sha256:'+hashlib.sha256(raw).hexdigest() for name,raw in encoded.items()}}
    encoded['manifest.json'] = (json.dumps(manifest,indent=2,sort_keys=True)+'\n').encode()
    return encoded


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend-dir',type=Path)
    parser.add_argument('--check',action='store_true')
    args = parser.parse_args()
    roots = [ROOT / 'contracts/v2.2-public-gbp']
    if args.frontend_dir:
        if not (args.frontend_dir / 'package.json').is_file(): raise SystemExit('Invalid frontend root')
        roots.append(args.frontend_dir / 'src/lib/report-capability/public-gbp-contracts')
    for directory in roots:
        for name,data in artifacts().items():
            path = directory / name
            if args.check:
                if not path.is_file() or path.read_bytes()!=data: raise SystemExit(f'Artifact differs: {path}')
            else:
                directory.mkdir(parents=True,exist_ok=True)
                path.write_bytes(data)
    print('Public GBP V1/V2 contracts and fixtures match.' if args.check else 'Exported public GBP V1/V2 contracts and fixtures.')


if __name__ == '__main__': main()
