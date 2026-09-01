"""Export deterministic synthetic site/public GBP alignment results."""
import argparse
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.jobs_v22.digest import request_digest
from app.report_v22.evidence_models import EvidenceBuildContext, EvidenceBuildInput, PublicGbpEvidenceSource, SiteEvidenceSource, SnapshotBinding
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.models import BusinessIdentity
from app.report_v22.public_gbp_snapshot import build_customer_public_gbp_snapshot
from app.report_v22.public_gbp_models import CustomerPublicGbpSnapshotInput
from app.report_v22.public_rule_catalog import GBP_ALIGNMENT_RULE_VERSION, GBP_RULES
from scripts.export_v22_evidence_fixtures import digest, encode, safe_path

INPUT_DIR = Path("tests/fixtures/report_v22_site_gbp_alignment/inputs")
OUTPUT_DIR = Path("tests/fixtures/report_v22_site_gbp_alignment/generated")
FRONTEND_DIR = Path("src/lib/report-v22/test-fixtures/site-gbp-alignment")
SITE_BASE = Path("tests/fixtures/report_v22_site_business/inputs/structured_single.json")
GBP_BASE = Path("tests/fixtures/report_v22_public_gbp/inputs/matched.json")
SCENARIOS = (
    "exact_match", "semantic_match", "multiple_candidates_one_match", "site_missing",
    "gbp_missing", "both_missing", "service_area_partial", "service_area_mismatch",
    "operating_model_not_applicable", "identity_unresolved", "source_unavailable",
    "comparison_time_gap",
)


def _script(records):
    return '<script type="application/ld+json">' + json.dumps(records, ensure_ascii=False, separators=(",", ":")) + "</script>"


def _record(**changes):
    value = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "@id": "#business",
        "name": "Fixture Plumbing",
        "telephone": "+1 512 555 0100",
        "address": "123 Fixture St, Austin, TX",
        "areaServed": ["Austin", "Round Rock"],
    }
    value.update(changes)
    return value


def _site(root, scenario):
    raw = json.loads((root / SITE_BASE).read_text())
    source = SiteEvidenceSource.model_validate_json(json.dumps(raw["source"]))
    if scenario == "semantic_match":
        html = _script(_record(name="Fixture Plumbing, LLC", telephone="(512) 555-0100",
                               address="123 Fixture Street, Austin, Texas",
                               areaServed=["Austin, TX", "Round Rock, TX"]))
    elif scenario == "multiple_candidates_one_match":
        html = _script([_record(name="Other Trading Name"), _record(name="Fixture Plumbing")])
    elif scenario in {"site_missing", "both_missing"}:
        html = _script(_record(telephone=None))
    elif scenario == "identity_unresolved":
        html = _script(_record(telephone=None)) + '<a href="tel:+15125550100">Call</a>'
    else:
        html = _script(_record())
    deep = source.payload.selected_pages[0].deep_snapshot
    deep.html = html
    deep.response_bytes = len(html.encode())
    deep.content_checksum = request_digest({"synthetic_alignment_html": html})
    if scenario == "comparison_time_gap":
        shift = timedelta(days=31)
        source.payload.started_at -= shift
        source.payload.completed_at -= shift
        deep.collected_at -= shift
        source.binding.fetched_at = source.payload.completed_at
    source.binding.payload_checksum = request_digest(source.payload)
    return source


def _public(root, scenario):
    base = json.loads((root / GBP_BASE).read_text())
    raw = deepcopy(base["snapshot_input"])
    fields = raw["record"]["fields"]
    fields["business_name"]["value"] = "Fixture Plumbing"
    fields["address"]["value"] = "123 Fixture St, Austin, TX"
    fields["phone"]["value"] = "+1 512 555 0100"
    fields["service_areas"]["value"] = ["Austin", "Round Rock"]
    fields["service_area_business"]["value"] = True
    if scenario in {"gbp_missing", "both_missing"}:
        fields["phone"] = {"state": "not_returned", "value": None}
    elif scenario == "service_area_partial":
        fields["service_areas"]["value"] = ["Austin", "Pflugerville"]
    elif scenario == "service_area_mismatch":
        fields["service_areas"]["value"] = ["Dallas"]
    if scenario == "source_unavailable":
        completed = __import__("datetime").datetime.fromisoformat(raw["completed_at"].replace("Z", "+00:00"))
        raw["expires_at"] = (completed + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    payload = build_customer_public_gbp_snapshot(CustomerPublicGbpSnapshotInput.model_validate_json(json.dumps(raw)))
    binding = SnapshotBinding(
        snapshot_id=UUID(base["snapshot_id"]),
        case_id=UUID(base["context"]["case_id"]),
        source_type="gbp",
        schema_version=payload.schema_version,
        payload_checksum=request_digest(payload),
        fetched_at=payload.completed_at,
        expires_at=payload.expires_at,
        health_status=payload.health_status,
        identity_match_status=payload.identity_match_status,
    )
    return PublicGbpEvidenceSource(binding=binding, payload=payload), base["context"]


def build_input(root, scenario):
    site = _site(root, scenario)
    public, context = _public(root, scenario)
    evidence = EvidenceBuildInput(context=EvidenceBuildContext.model_validate_json(json.dumps(context)), sources=[public, site])
    identity = BusinessIdentity(
        business_name="Fixture Plumbing LLC",
        site_url=evidence.context.site_url,
        normalized_domain="example.test",
        operating_model="storefront" if scenario == "operating_model_not_applicable" else "hybrid",
        primary_location=evidence.context.target_market,
        public_gbp_url=evidence.context.customer_public_gbp.public_gbp_url,
    )
    return PublicFindingsInput(evidence_input=evidence, business_identity=identity)


def build_resources(root):
    root = root.resolve()
    directory = root / INPUT_DIR
    safe_path(root, directory)
    expected = {f"{name}.json" for name in SCENARIOS}
    if not directory.is_dir() or {path.name for path in directory.iterdir()} != expected:
        raise ValueError("Unexpected site/GBP alignment input files")
    inputs, outputs, states, finding_counts = {}, {}, {}, {}
    for scenario in SCENARIOS:
        path = directory / f"{scenario}.json"
        safe_path(root, path)
        raw = path.read_bytes()
        descriptor = json.loads(raw)
        if descriptor != {"scenario": scenario}:
            raise ValueError("Invalid site/GBP alignment descriptor")
        result = build_public_findings(build_input(root, scenario))
        filename = f"{scenario}.json"
        inputs[filename] = digest(raw)
        outputs[filename] = encode(result.model_dump(mode="json"))
        evaluations = [item for item in result.rule_evaluations if item.rule_id in GBP_RULES]
        states[filename] = {item.rule_id: {"state": item.state, "reason": item.reason} for item in evaluations}
        finding_counts[filename] = sum(item.rule_id in GBP_RULES for item in result.findings)
    manifest = {
        "version": 1,
        "rule_version": GBP_ALIGNMENT_RULE_VERSION,
        "synthetic_snapshots": True,
        "inputs": inputs,
        "files": {name: digest(data) for name, data in outputs.items()},
        "states": states,
        "finding_counts": finding_counts,
    }
    return {**outputs, "manifest.json": encode(manifest)}


def sync_resources(root, frontend=None, *, check=False):
    root = root.resolve()
    artifacts = build_resources(root)
    destinations = [(root, root / OUTPUT_DIR)]
    if frontend is not None:
        frontend = frontend.resolve()
        if frontend == root or not (frontend / "package.json").is_file():
            raise ValueError("Frontend must be a separate frontend repository")
        destinations.append((frontend, frontend / FRONTEND_DIR))
    for repository, directory in destinations:
        safe_path(repository, directory)
        if directory.exists() and (not directory.is_dir() or {path.name for path in directory.iterdir()} - set(artifacts)):
            raise ValueError("Unexpected site/GBP alignment output files")
        for name, data in artifacts.items():
            path = directory / name
            safe_path(repository, path)
            if path.exists() and not path.is_file():
                raise ValueError("Unsafe site/GBP alignment output target")
            if check and (not path.is_file() or path.read_bytes() != data):
                raise ValueError("V22_SITE_GBP_ALIGNMENT_FIXTURE_DRIFT: " + str(path.relative_to(repository)))
    if check:
        return
    for _, directory in destinations:
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in artifacts.items():
            path = directory / name
            if path.is_file() and path.read_bytes() == data:
                continue
            with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{name}-", delete=False) as handle:
                handle.write(data)
                temporary = Path(handle.name)
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        sync_resources(ROOT, args.frontend_dir, check=args.check)
    except (ValueError, OSError, FindingsError):
        print("Site/GBP alignment fixtures could not be generated or verified.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
