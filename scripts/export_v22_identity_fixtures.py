#!/usr/bin/env python3
"""P1-05 additive identity/report fixtures. Never rewrites older fixture sets."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.identity_input import prepare_identity_input
from app.report_v22.capability_public import build_public_capability_report
from test_p1_05_identity import bound, confirmation


def artifacts():
 m,q=bound()
 m,q,_,_=prepare_identity_input(m,q,confirmation=confirmation(q,'service_area'))
 return {'identity.json':canonical_json_bytes(q.business_identity)+b'\n',
         'report.json':canonical_json_bytes(build_public_capability_report(m,q))+b'\n'}


def main():
 p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');p.add_argument('--frontend-dir',type=Path)
 args=p.parse_args();roots=[ROOT/'tests/fixtures/p1-05-contracts']
 if args.frontend_dir:roots.append(args.frontend_dir/'src/lib/report-capability/fixtures/identity-v2')
 for root in roots:
  for name,data in artifacts().items():
   path=root/name
   if args.check:
    if not path.is_file() or path.read_bytes()!=data:raise SystemExit('Identity fixture drift')
   else:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
 print(f'identity fixtures: check={args.check}, files=2, destinations={len(roots)}, ok')
if __name__=='__main__':main()
