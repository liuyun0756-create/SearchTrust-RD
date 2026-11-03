"""Replay frozen Nova v5 locally into a separate labelled report for UI tests."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability_finding_status import PUBLIC_RULESET


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.resolve().is_relative_to(args.input_dir.resolve()) or args.output.exists():
        raise SystemExit('Choose a new output file outside frozen inputs')
    def read(name):return json.loads((args.input_dir/name).read_text())
    old=read('nova-v5-report.json')['payload']
    rows={r['id']:r for r in read('nova-v5-snapshots.json')}
    request=deepcopy(read('nova-v5-generation-recipe.json')['recipe']['request'])
    for source in request['evidence_input']['sources']:
        source['payload']=rows[source['binding']['snapshot_id']]['normalized_payload']
        if source.get('shared_snapshot') is not None:source['shared_snapshot']['snapshot']=source['payload']
    assert canonical_json_bytes(build_public_capability_report(old['input_manifest'],request))==canonical_json_bytes(old)
    manifest=deepcopy(old['input_manifest']);manifest['ruleset_version']=PUBLIC_RULESET
    manifest['input_checksum']=verified_request_digest({k:v for k,v in manifest.items() if k!='input_checksum'})
    report=build_public_capability_report(manifest,request)
    args.output.write_bytes(canonical_json_bytes(report))
    print(json.dumps({'frozen_v5_replay_byte_identical':True,'output':str(args.output),'external_calls':0,
        'actions':[a.title for a in report.actions]}))


if __name__=='__main__':main()
