"""Local-only audit of saved job checkpoints; never invokes a provider or database RPC."""
import argparse, asyncio, copy, gzip, hashlib, json, os, sys
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser(description='Offline structural-reuse audit; saved data only, provider calls forbidden.')
parser.add_argument('--frozen-export', type=Path, required=True)
parser.add_argument('--redis-export', type=Path, required=True)
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args()
p = args.output_dir.resolve()
assert any((x in p.parts for x in ['.release-private', 'output'])), 'Output must stay in a private or ignored artifact directory'
os.umask(0o077)
p.mkdir(parents=True, exist_ok=True, mode=0o700)
from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.site_inventory_stage import CheckpointedSiteFetcher, _CheckpointedFirecrawl, DEEP_STRUCTURE_CHECKPOINT_VERSION
from app.collectors.site_inventory import SiteInventoryCollector
from app.collectors.site_inventory_models import SiteInventorySnapshotV3
from app.report_v22.evidence_adapters.site_counts import eligible_html
from app.report_v22.capability import AnalysisInputManifest
from app.report_v22.findings_models import PublicFindingsInput
from app.jobs_v22.capability_inputs import generation_recipe
from app.jobs_v22.freeze_safety import freeze_json_bytes
from app.jobs_v22.cost_models import CostLedgerState, cost_counters_from_state
from app.report_v22.operating_model import inspect_operating_model
frozen = args.frozen_export.resolve()
export = args.redis_export.resolve()
original_hashes = {x: hashlib.sha256(x.read_bytes()).hexdigest() for x in [frozen, export]}
f = json.loads(frozen.read_text())['rows'][0]
m = AnalysisInputManifest.model_validate_json(json.dumps(f['manifest']))
q = PublicFindingsInput.model_validate_json(json.dumps(f['request']))
original = next((s.payload for s in q.evidence_input.sources if s.kind == 'site'))
entries = json.loads(gzip.decompress(export.read_bytes()))['keys']
raw = {key: json.loads(v['value']['data']) for (key, v) in entries.items() if v['value']['enc'] == 'utf8'}

class Checkpoints:

    def __init__(self):
        self.new = {}
        self.writes = []
        self.raw_hits = 0

    async def get(self, job, key):
        if key in self.new:
            return copy.deepcopy(self.new[key])
        digest = hashlib.sha256(key.encode()).hexdigest()
        matches = [v for (k, v) in raw.items() if f':job:{job}:checkpoint:' in k and k.endswith(':checkpoint:' + digest)]
        if matches:
            self.raw_hits += 1
            assert len(matches) == 1
            return copy.deepcopy(matches[0])
        return None

    async def run_once(self, job, key, operation):
        value = await self.get(job, key)
        if value is None:
            value = await operation()
            self.new[key] = copy.deepcopy(value)
            self.writes.append(key)
        return value

def clock():
    ds = {str(s.url): s.deep_snapshot.collected_at for s in original.selected_pages if s.deep_snapshot}
    values = iter([original.started_at, *[ds[str(a.url)] for a in original.deep_attempts if a.succeeded], original.completed_at])
    return lambda : next(values)

async def run(reuse):
    cp = Checkpoints()
    delegate = AsyncMock()
    delegate.apply_crawl_delay = Mock()
    delegate.fetch.side_effect = AssertionError('UNSAVED_HTTP_CALL')
    delegate.map.side_effect = AssertionError('UNSAVED_FIRECRAWL_CALL')
    ns = DEEP_STRUCTURE_CHECKPOINT_VERSION if reuse else None
    fetcher = CheckpointedSiteFetcher(delegate=delegate, checkpoints=cp, job_id=m.job_id, checkpoint_namespace=ns)
    mapper = _CheckpointedFirecrawl(delegate=delegate, checkpoints=cp, job_id=m.job_id, checkpoint_namespace=ns)
    collector = SiteInventoryCollector(fetcher=fetcher, firecrawl=mapper, structural_max_bytes=256000, deep_max_bytes=2000000, sitemap_max_bytes=2000000, sitemap_decompressed_max_bytes=10000000, sitemap_max_files=20, sitemap_max_depth=2, batch_size=25, resource_limits=True, sampling_quality=True, contact_coverage=True, reuse_deep_structure=reuse, clock=clock())
    t = q.evidence_input.context.target_market
    market = ' '.join((v for v in [t.display_name, t.city, t.region, t.country_code] if v))
    result = await collector.collect(site_url=str(original.root_url), discovery_limit=original.discovery_limit, deep_analysis_limit=original.deep_analysis_limit, primary_service=m.business.primary_service, target_market=market)
    delegate.fetch.assert_not_called()
    delegate.map.assert_not_called()
    return (result, cp)

def rpc_bytes(site, *, counters=None):
    r = q.model_copy(deep=True)
    manifest = m.model_dump(mode='json')
    source = next((s for s in r.evidence_input.sources if s.kind == 'site'))
    source.payload = site
    source.binding.payload_checksum = request_digest(site)
    assert r.business_identity.operating_model.confirmation_ref is None, 'Explicit identity confirmation requires review before counterfactual reconstruction'
    r.business_identity.operating_model = inspect_operating_model(r)[0]
    manifest['identity_checksum'] = request_digest(r.business_identity)
    for ref in manifest['snapshots']:
        ref['payload_checksum'] = next((s.binding.payload_checksum for s in r.evidence_input.sources if s.kind == ref['source_kind']))
    manifest['generation_input_checksum'] = verified_request_digest(r.model_dump(mode='json'))
    manifest['input_checksum'] = verified_request_digest({k: v for (k, v) in manifest.items() if k != 'input_checksum'})
    (nm, nq, recipe) = generation_recipe(manifest, r)
    body = dict(p_user_id=str(nm.user_id), p_case_id=str(nm.case_id), p_job_id=str(nm.job_id), p_manifest=nm.model_dump(mode='json'), p_manifest_canonical=verified_canonical_json_bytes(nm.model_dump(mode='json', exclude={'input_checksum'})).decode(), p_recipe=recipe, p_generation_canonical=verified_canonical_json_bytes(nq.model_dump(mode='json')).decode(), p_public_source_canonicals={s.kind: canonical_json_bytes(s.payload).decode() for s in nq.evidence_input.sources}, p_previous_canonical=None, p_run_generation=1)
    return len(freeze_json_bytes(dict(p_lease_token='00000000-0000-0000-0000-000000000000', p_cost_counters=counters or {}, p_input=body)))

async def main():
    with patch('httpx.AsyncClient.send', side_effect=AssertionError('NO_NETWORK')) as provider:
        (baseline, _) = await run(False)
        (current, cp) = await run(True)
        assert canonical_json_bytes(baseline) == canonical_json_bytes(original), 'BASELINE_REPLAY_DRIFT'
        assert sum(map(eligible_html, baseline.pages)) == 1 and sum(map(eligible_html, current.pages)) == 16
        assert [str(s.url) for s in baseline.selected_pages] == [str(s.url) for s in current.selected_pages]
        assert [(s.deep_snapshot.html, s.deep_snapshot.content_checksum) for s in baseline.selected_pages] == [(s.deep_snapshot.html, s.deep_snapshot.content_checksum) for s in current.selected_pages]
        (a, b) = (rpc_bytes(baseline), rpc_bytes(current))
        state = CostLedgerState.model_validate_json(canonical_json_bytes(next((v for v in raw.values() if v.get('schema_version') == 'v22_cost_ledger_v1'))))
        counters = cost_counters_from_state(state, now=state.updated_at).root
        (total_before, total_after) = (rpc_bytes(baseline, counters=counters), rpc_bytes(current, counters=counters))
        assert total_after <= 40000000
        provider.assert_not_called()
        assert all((original_hashes[x] == hashlib.sha256(x.read_bytes()).hexdigest() for x in original_hashes))
        result = {'eligible_before': 1, 'eligible_after': 16, 'baseline_byte_identical': True, 'page_types_before': dict(Counter((x.page_type for x in baseline.pages))), 'page_types_after': dict(Counter((x.page_type for x in current.pages))), 'selection_urls_unchanged': True, 'deep_html_and_checksums_unchanged': True, 'selected_urls': [str(s.url) for s in current.selected_pages], 'rpc_bytes_before': a, 'rpc_bytes_after': b, 'rpc_delta': b - a, 'rpc_with_saved_counters_before': total_before, 'rpc_with_saved_counters_after': total_after, 'provider_calls': provider.call_count, 'new_raw_checkpoint_writes': len(cp.writes), 'namespace': DEEP_STRUCTURE_CHECKPOINT_VERSION, 'freeze_executed': False, 'cost_counters': 'Both empty-counter and saved final-counter totals are reported; no RPC executed'}
        (p / 'replay-result.json').write_text(json.dumps(result, indent=2))
        (p / 'replayed-site.json').write_bytes(canonical_json_bytes(current))
        print(json.dumps(result, indent=2))
asyncio.run(main())
