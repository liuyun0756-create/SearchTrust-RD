#!/usr/bin/env python3
"""Reproduce v11 reader fixtures from untouched gold input; never label truth."""
import argparse
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.eval_gold import generate,read_json
from app.jobs_v22.digest import canonical_json_bytes


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--frontend-dir',type=Path)
    parser.add_argument('--private-root',type=Path,default=ROOT.parent/'.release-private/2026-10-29')
    args=parser.parse_args()
    destinations=[ROOT/'tests/fixtures/p1-00b-hours']
    if args.frontend_dir:destinations.append(args.frontend_dir/'src/lib/report-capability/fixtures/opening-hours')
    for name in ('all-star-plumbing-fresno','vision-tech-swansea'):
        directory=ROOT/'tests/fixtures/gold'/name
        report,*_=generate(read_json(directory/'input.json'),directory,args.private_root,ruleset='v22_capability_public_v11')
        body=canonical_json_bytes(report)+b'\n'
        for dest in destinations:
            file=dest/(name+'.json')
            if args.check:
                if not file.is_file() or file.read_bytes()!=body:raise SystemExit('Hours fixture drift: '+name)
            else:
                dest.mkdir(parents=True,exist_ok=True);file.write_bytes(body)
    print('Opening-hours fixtures: 2 reports, '+str(len(destinations))+' destinations, check='+str(args.check))

if __name__=='__main__':main()
