#!/usr/bin/env python3
"""Local-only browser stub: actual pure service, saved sources, no network/storage."""
import asyncio
import json
from pathlib import Path
import socket
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
def forbidden(*args,**kwargs):raise AssertionError('Preview network forbidden')
socket.socket.connect=forbidden
socket.create_connection=forbidden
from scripts.prepare_p1_05_review_preview import Memory
from app.preflight_v22.identity_review import IdentityReviewService,IdentityReviewCommand
from app.report_v22.public_reassessment import validated_base
from datetime import datetime,timezone

async def main():
 name=sys.argv[1]
 if name not in {'pr-plumbing-fresno','volcanoes-hiking-guides'}:raise ValueError('scenario')
 artifact=json.loads((ROOT.parent/'search-trust/output/playwright/p1-05'/f'{name}.json').read_text())
 r,q=validated_base(artifact['base'],artifact['request'])
 records=[]
 if name=='volcanoes-hiking-guides':
  sys.path.insert(0,str(ROOT/'tests'))
  from test_p1_05_gbp_identity import saved_records
  records=[x.model_dump(mode='json') for x in saved_records(name,'initial')]
 command=json.loads(sys.stdin.buffer.read(8193))
 command.update(user_id=str(r.input_manifest.user_id),case_id=str(r.input_manifest.case_id),report_id=str(r.id))
 if command['operation'] not in {'workspace','preview'}:raise ValueError('operation')
 service=IdentityReviewService(Memory(r,q,records),clock=lambda:datetime(2026,11,5,0,tzinfo=timezone.utc))
 result=await service.run(IdentityReviewCommand.model_validate_json(json.dumps(command)))
 print(json.dumps(result,default=lambda v:v.isoformat() if isinstance(v,datetime) else str(v)))
if __name__=='__main__':asyncio.run(main())
