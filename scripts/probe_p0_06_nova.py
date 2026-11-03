"""Read-only local replay of Nova's frozen v5 inputs; no collection or writes."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability_finding_status import PUBLIC_RULESET


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input-dir', type=Path, required=True)
    args = parser.parse_args()
    def read(name):
        return json.loads((args.input_dir / name).read_text())
    stored = read('nova-v5-report.json')['payload']
    rows = {r['id']: r for r in read('nova-v5-snapshots.json')}
    request = deepcopy(read('nova-v5-generation-recipe.json')['recipe']['request'])
    for source in request['evidence_input']['sources']:
        source['payload'] = rows[source['binding']['snapshot_id']]['normalized_payload']
        if source.get('shared_snapshot') is not None:
            source['shared_snapshot']['snapshot'] = source['payload']
    before = build_public_capability_report(stored['input_manifest'], request)
    assert canonical_json_bytes(before) == canonical_json_bytes(stored)
    manifest = deepcopy(stored['input_manifest'])
    manifest['ruleset_version'] = PUBLIC_RULESET
    manifest['input_checksum'] = verified_request_digest({k:v for k,v in manifest.items() if k!='input_checksum'})
    after = build_public_capability_report(manifest, request)
    rule = 'v22_public.market_site_domain_unobserved'
    old = next(f for f in before.findings if f.rule_id==rule)
    new = next(f for f in after.findings if f.rule_id==rule)
    print(json.dumps({'frozen_v5_replay_byte_identical': True,
        'before': {'report_schema':before.schema_version, 'classification':old.classification, 'labels_present':old.kind is not None},
        'after': {'report_schema':after.schema_version,'ruleset':PUBLIC_RULESET,'kind':new.kind,'status':new.status,
            'coverage':[c.model_dump(mode='json') for c in new.coverage]},
        'external_calls':0},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
