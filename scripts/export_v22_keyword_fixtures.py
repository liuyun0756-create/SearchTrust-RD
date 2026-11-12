#!/usr/bin/env python3
"""Additive synthetic V14 examples; no historical fixtures or network writes."""
import argparse
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'tests')]
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.capability_inputs import generation_recipe
from app.report_v22.capability_public import build_public_capability_report
from test_p1_01_keyword_visibility import keyword_input


def artifacts():
    m,q = keyword_input((True,False,None))
    _,_,recipe = generation_recipe(m,q)
    return {'mixed.json':canonical_json_bytes(build_public_capability_report(m,q))+b'\n',
        'bound.json':canonical_json_bytes(dict(manifest=m.model_dump(mode="json"), request=q.model_dump(mode="json"), recipe=recipe))+b'\n'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');p.add_argument('--frontend-dir',type=Path)
    args=p.parse_args();roots=[ROOT/'tests/fixtures/p1-01-keywords']
    if args.frontend_dir:roots.append(args.frontend_dir/'src/lib/report-capability/fixtures/keywords-v14')
    for root in roots:
        for name,data in artifacts().items():
            path=root/name
            if args.check:
                if not path.is_file() or path.read_bytes()!=data:raise SystemExit('Keyword fixture drift')
            else:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    print(f'keyword fixtures: check={args.check}, files=2, destinations={len(roots)}, ok')
if __name__=='__main__':main()
