"""P0-06 generated fixtures, additive to all frozen historical report fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_p0_06_finding_status import labelled_public_fixture, labelled_service_fixture, contact_report_fixture


def artifacts():
    values={'public.json':labelled_public_fixture(search_gap=True)[2].model_dump(mode='json'),
            'service.json':labelled_service_fixture()[2].model_dump(mode='json')}
    values.update({f'contact-{case}.json':contact_report_fixture(case)[0].model_dump(mode='json')
                   for case in ('structured','text_pattern','rendered','not_checked','mismatch')})
    result={name:(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode() for name,value in values.items()}
    result['manifest.json']=(json.dumps({'schema_version':'2.2.5','files':{name:'sha256:'+hashlib.sha256(raw).hexdigest() for name,raw in result.items()}},sort_keys=True,indent=2)+'\n').encode()
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--frontend-dir',type=Path);p.add_argument('--check',action='store_true');args=p.parse_args()
    roots=[ROOT/'tests/fixtures/p0_06/generated']
    if args.frontend_dir: roots.append(args.frontend_dir/'src/lib/report-capability/fixtures/labelled')
    outputs=artifacts()
    for root in roots:
        for name,raw in outputs.items():
            if args.check:
                if not (root/name).is_file() or (root/name).read_bytes()!=raw: raise SystemExit('P0-06 fixture drift')
            else:
                root.mkdir(parents=True,exist_ok=True);(root/name).write_bytes(raw)
    print(json.dumps({'ok':True,'check':args.check,'files':len(outputs),'destinations':len(roots)}))


if __name__=='__main__': main()
