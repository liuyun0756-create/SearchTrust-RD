#!/usr/bin/env python3
"""Offline preview artifacts from frozen inputs; no credentials/network or truth edits."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid5,NAMESPACE_URL
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from app.jobs_v22.digest import canonical_json_bytes,verified_request_digest
from app.jobs_v22.capability_inputs import generation_recipe
from app.preflight_v22.identity_review import IdentityReviewService,IdentityReviewCommand
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.identity_input import prepare_identity_input
from scripts.eval_gold import generate,read_json

class Memory:
 def __init__(self,report,request,records=()):
  self.bundle=dict(report=report.model_dump(mode='json'),request=request.model_dump(mode='json'),identity_revision=0,delivery_revision=0,saved_records=records)
 async def call(self,op,command,**kw):
  if op=='workspace':return self.bundle
  if op=='prepared':return None
  if op=='preview':return {**kw['p_preview'],'request_id':kw['p_request_id'],'state':'preview'}
  raise AssertionError(op)

async def artifact(name,report,request,records=()):
 repo=Memory(report,request,records)
 service=IdentityReviewService(repo,clock=lambda:datetime(2026,11,5,0,tzinfo=timezone.utc))
 owner=dict(user_id=report.input_manifest.user_id,case_id=report.input_manifest.case_id,report_id=report.id)
 workspace=await service.run(IdentityReviewCommand(operation='workspace',**owner))
 key=next((b['ref'] for b in workspace['branches'] if b['available_for_reassessment']),None)
 if not key:raise ValueError(f'{name}: no saved eligible branch')
 variants={}
 for variant,mode,visit in [(m,m,False) for m in ['storefront','service_area','hybrid','meeting_point']]+[(m+'_visit',m,True) for m in ['storefront','hybrid']]:
  cmd=IdentityReviewCommand(operation='preview',**owner,request_id=uuid5(NAMESPACE_URL,f'p105-preview:{name}:{variant}'),
   base_checksum=workspace['base_checksum'],selection=dict(operating_model=mode,branch_ref=key,reason='Confirmed with the business owner',customers_can_visit=visit))
  variants[variant]=await service.run(cmd)
 _,_,recipe=generation_recipe(report.input_manifest,request)
 return dict(scenario=name,base=report.model_dump(mode='json'),request=request.model_dump(mode='json'),recipe=recipe,
  workspace=workspace,variants=variants,external_calls=0)

async def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,required=True);parser.add_argument('--synthetic',action='store_true');args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
 results=[]
 if args.synthetic:
  from test_p1_05_identity import bound
  m,q=bound();m,q,_,_=prepare_identity_input(m,q);report=build_public_capability_report(m,q)
  results.append(('synthetic',await artifact('synthetic',report,q)))
 else:
  for name in ['pr-plumbing-fresno','volcanoes-hiking-guides']:
   directory=ROOT/'tests/fixtures/gold'/name
   before,_,request,*_=generate(read_json(directory/'input.json'),directory,ROOT.parent/'.release-private/2026-10-29',ruleset='v22_capability_public_v12')
   m,q,_,_=prepare_identity_input(before.input_manifest,request);report=build_public_capability_report(m,q)
   records=[]
   if name=='volcanoes-hiking-guides':
    # Saved P0-09 search results provide both branches, but do not impersonate
    # full GBP detail snapshots or replace the frozen bound source.
    from test_p1_05_gbp_identity import saved_records
    records=[r.model_dump(mode='json') for r in saved_records('volcanoes-hiking-guides','initial')]
   results.append((name,await artifact(name,report,q,records)))
 for name,value in results:
  raw=canonical_json_bytes(value)+b'\n';(args.output_dir/f'{name}.json').write_bytes(raw)
  print(json.dumps(dict(scenario=name,bytes=len(raw),branches=len(value['workspace']['branches']),external_calls=0)))
if __name__=='__main__':asyncio.run(main())
