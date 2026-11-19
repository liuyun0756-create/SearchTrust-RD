"""P1-07 synthetic complete pools; no old fixtures are rewritten."""
import argparse
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_p1_07_candidates import candidate_report
from test_p1_09_action_advice import example_gold_input, select_first_example
from app.report_v22.capability_public import build_public_capability_report
from app.jobs_v22.digest import canonical_json_bytes

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');parser.add_argument('--frontend-dir',type=Path);args=parser.parse_args()
    roots=[ROOT/'tests/fixtures/p1-07-candidates']
    if args.frontend_dir:roots.append(args.frontend_dir/'src/lib/report-capability/fixtures/candidates')
    values={name:canonical_json_bytes(candidate_report(**options))+b'\n' for name,options in {'five.json':{},'dependencies.json':{'dependent':True},'empty.json':{'count':0}}.items()}
    values['v20.json']=canonical_json_bytes(select_first_example(build_public_capability_report(*example_gold_input())))+b'\n'
    for root in roots:
        if args.check:
            if {p.name:p.read_bytes() for p in root.glob('*.json')}!=values:raise SystemExit('Candidate fixture drift')
        else:
            root.mkdir(parents=True,exist_ok=True)
            for name,raw in values.items():(root/name).write_bytes(raw)
    print(f'Candidate fixtures: {len(values)} reports, {len(roots)} destinations, check={args.check}')
if __name__=='__main__':main()
