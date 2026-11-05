#!/usr/bin/env python3
"""Offline v11/v12 comparison, with no truth, snapshot or report writes."""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.eval_gold import generate,read_json
from app.jobs_v22.digest import canonical_json_bytes

def compare():
    rows=[]
    for p in sorted((ROOT/'tests/fixtures/gold').glob('*/input.json')):
        old,*_=generate(read_json(p),p.parent,ROOT.parent/'.release-private/2026-10-29',ruleset='v22_capability_public_v11')
        new,*_=generate(read_json(p),p.parent,ROOT.parent/'.release-private/2026-10-29',ruleset='v22_capability_public_v12')
        observations_equal=all(canonical_json_bytes(old.model_dump(mode='json')[k])==canonical_json_bytes(new.model_dump(mode='json')[k]) for k in ('findings','evidence','contact_checks'))
        old_visible=[a for a in old.actions if any(f.status=='confirmed' and f.confidence!='low' for f in old.findings if f.id in a.finding_ids)]
        row=dict(site_id=p.parent.name,observations_byte_identical=observations_equal,
                 v11_system_order=[a.id for a in old.actions],v12_system_order=[a.id for a in new.actions],
                 v11_client_order=[a.title for a in old_visible],v12_client_order=[a.title for a in new.actions],
                 client_order_identical=[a.id for a in old_visible]==[a.id for a in new.actions],
                 candidate_count=len(new.action_candidates),all_candidates_preserved={a.id for a in new.action_candidates}=={e.input.action_id for e in old.prioritization.candidates})
        rows.append(row)
    return dict(external_calls=0,cases=rows,ok=all(r['observations_byte_identical'] and r['client_order_identical'] and r['all_candidates_preserved'] for r in rows))

if __name__=='__main__':
    import socket
    def offline(*args,**kwargs):raise RuntimeError('Network forbidden during comparison')
    socket.socket.connect=offline;socket.create_connection=offline;socket.getaddrinfo=offline
    result=compare();print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if result['ok'] else 1)
