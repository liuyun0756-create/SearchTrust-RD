import json
from pathlib import Path

from app.report_v22.evidence import build_evidence_index
from evidence_helpers import site_source, bind, build_input


def test_real_zero_counts_are_traceable_and_exclude_http_error_pages():
    source = site_source()
    source.payload.pages[0].status_code = 404
    source.binding = bind(source.payload, "site", 11)
    result = build_evidence_index(build_input(source))
    counts = [(e, t) for e, t in zip(result.evidence_index, result.source_traces) if t.selector.field.startswith("eligible_")]
    assert len(counts) == 4
    assert all(e.normalized_value == 0 for e, _ in counts)
    assert all(t.origin_paths == ["/payload/completed_at", "/payload/pages"] for _, t in counts)


def test_existing_fixture_observations_keep_their_ids_and_contents():
    from hashlib import sha256
    from app.jobs_v22.digest import canonical_json_bytes
    from app.report_v22.evidence_models import EvidenceBuildInput
    base = Path(__file__).parent / "fixtures/report_v22_evidence"
    before = json.loads((base.parent / "report_v22_findings/legacy-evidence-hashes.json").read_text())
    request = EvidenceBuildInput.model_validate_json((base / "inputs/public.json").read_bytes())
    after = {e.evidence_id: e.model_dump(mode="json") for e in build_evidence_index(request).evidence_index}
    assert len(before) == 88
    assert all("sha256:" + sha256(canonical_json_bytes(after[identifier])).hexdigest() == digest for identifier, digest in before.items())
