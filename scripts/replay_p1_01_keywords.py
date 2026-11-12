#!/usr/bin/env python3
"""Read-only six-site keyword replay. Socket and HTTP provider calls are denied."""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.eval_gold import generate,read_json
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.identity_input import prepare_identity_input
from app.report_v22.capability_public import build_public_capability_report


def main():
 p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,required=True)
 p.add_argument('--private-root',type=Path,default=ROOT.parent/'.release-private/2026-10-29')
 p.add_argument('--historical-only',action='store_true');p.add_argument('--compare-history',type=Path)
 args=p.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
 files=[f for f in (ROOT/'tests/fixtures/gold').rglob('*') if f.is_file()]
 files += [args.private_root/name for name in ['nova-v5-report.json','nova-v5-snapshots.json','nova-v5-generation-recipe.json']]
 before={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
 confirmations=read_json(ROOT/'tests/fixtures/p1-05-identity/confirmations.json')
 history={};sites=[]
 with ExitStack() as stack:
  denied=[stack.enter_context(patch(name,side_effect=AssertionError('P1_01_OFFLINE_NO_PROVIDER_CALL'))) for name in
   ['socket.create_connection','socket.getaddrinfo','httpx.Client.send','httpx.AsyncClient.send']]
  for site,confirmation in sorted(confirmations.items()):
   directory=ROOT/'tests/fixtures/gold'/site
   old,_,q,*_=generate(read_json(directory/'input.json'),directory,args.private_root,ruleset='v22_capability_public_v12')
   old_bytes=canonical_json_bytes(old);request_bytes=canonical_json_bytes(q)
   m13,q13,_,_=prepare_identity_input(old.input_manifest,q,confirmation=confirmation)
   old13=build_public_capability_report(m13,q13)
   history[site]={'v12':hashlib.sha256(old_bytes).hexdigest(),'v13':hashlib.sha256(canonical_json_bytes(old13)).hexdigest()}
   if args.historical_only:continue
   from app.report_v22.keyword_visibility import prepare_keyword_input
   m14,q14=prepare_keyword_input(m13,q13)
   new=build_public_capability_report(m14,q14)
   assert canonical_json_bytes(old)==old_bytes and canonical_json_bytes(q)==request_bytes
   assert new.findings==old13.findings and new.actions==old13.actions and new.evidence==old13.evidence
   module=new.keyword_visibility.model_dump(mode='json')
   sites.append({'site':site,'rows':module['rows'],'local_pack':module['local_pack'],'organic':module['organic'],
     'findings':len(new.findings),'actions':len(new.actions),'action_state':new.action_state})
   (args.output_dir/f'{site}.json').write_bytes(canonical_json_bytes(new)+b'\n')
  assert all(mock.call_count==0 for mock in denied)
 if args.compare_history:
  assert history==read_json(args.compare_history),'HISTORICAL_V12_V13_CHANGED'
 assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==value for path,value in before.items())
 (args.output_dir/'historical-digests.json').write_text(json.dumps(history,indent=2)+'\n')
 result={'external_calls':0,'source_files_unchanged':len(before),'historical_v12_v13_compared':bool(args.compare_history),'sites':sites}
 (args.output_dir/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 if sites:
  lines=['| Site | Query | Map Pack | Organic first page | Maps (advisor only) |','|---|---|---|---|---|']
  for site in sites:
   for row in site['rows']:
    cells=[]
    for channel in ['local_pack','organic','maps']:
     item=row[channel];cells.append(f"{item['status']}"+(f" #{item['position']}" if item['position'] is not None else '')+f" ({item['reason']})")
    lines.append('| '+ ' | '.join([site['site'],row['query'],*cells])+' |')
  (args.output_dir/'six-site-table.md').write_text('\n'.join(lines)+'\n')
 print(json.dumps({'sites':len(history),'external_calls':0,'historical_v12_v13_compared':bool(args.compare_history),'source_files_unchanged':len(before)},ensure_ascii=False))
if __name__=='__main__':main()
