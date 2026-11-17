"""Synthetic v17 examples only; frozen historical fixtures remain untouched."""
import argparse
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_p1_04_comparison_reasons import reasons_input
from app.report_v22.capability_public import build_public_capability_report
from app.jobs_v22.capability_inputs import generation_recipe
from app.jobs_v22.digest import canonical_json_bytes


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--frontend-dir',type=Path);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    m,q=reasons_input();_,_,recipe=generation_recipe(m,q)
    files={'report.json':canonical_json_bytes(build_public_capability_report(m,q))+b'\n',
           'bound.json':canonical_json_bytes(dict(manifest=m.model_dump(mode='json'),request=q.model_dump(mode='json'),recipe=recipe))+b'\n'}
    roots=[ROOT/'tests/fixtures/p1-04-reasons']
    if args.frontend_dir:roots.append(args.frontend_dir/'src/lib/report-capability/fixtures/reasons-v17')
    for root in roots:
        for name,data in files.items():
            target=root/name
            if args.check:
                if not target.exists() or target.read_bytes()!=data:raise SystemExit('Possible reason fixture drift')
            else:root.mkdir(exist_ok=True,parents=True);target.write_bytes(data)
    print(f'Possible reason fixtures: check={args.check}, files=2, destinations={len(roots)}, ok')

if __name__=='__main__':main()
