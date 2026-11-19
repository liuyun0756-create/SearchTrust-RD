"""Generate synthetic V18 examples, independently of historical fixtures."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from app.jobs_v22.capability_inputs import generation_recipe
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.capability_public import build_public_capability_report
from test_p1_08_local_entity_coverage import local_entity_input


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend-dir', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    manifest, request = local_entity_input()
    _, _, recipe = generation_recipe(manifest, request)
    values = {
        'bound.json': dict(manifest=manifest.model_dump(mode='json'), request=request.model_dump(mode='json'), recipe=recipe),
        'report.json': build_public_capability_report(manifest, request),
    }
    target = args.frontend_dir / 'src/lib/report-capability/fixtures/local-entity-v18'
    if not args.check:
        target.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        path = target / name
        data = canonical_json_bytes(value) + b'\n'
        if args.check:
            if path.read_bytes() != data:
                raise SystemExit(f'Local entity fixture drift: {name}')
        else:
            path.write_bytes(data)
    print('V18 local entity fixtures verified' if args.check else 'V18 local entity fixtures generated')


if __name__ == '__main__':
    main()
