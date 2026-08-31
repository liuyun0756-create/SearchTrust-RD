"""Build synthetic customer public GBP snapshots and export frozen fragments only."""
import argparse
import os
from pathlib import Path
import sys
import tempfile
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.jobs_v22.digest import request_digest
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.evidence_models import EvidenceBuildContext, EvidenceBuildInput, PublicGbpEvidenceSource, SnapshotBinding
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.models import StrictModel
from app.report_v22.public_gbp_errors import PublicGbpError
from app.report_v22.public_gbp_models import CustomerPublicGbpSnapshotInput, IDENTITY_VERSION
from app.report_v22.public_gbp_snapshot import build_customer_public_gbp_snapshot
from scripts.export_v22_evidence_fixtures import digest, encode, safe_path

INPUT_DIR = Path("tests/fixtures/report_v22_public_gbp/inputs")
OUTPUT_DIR = Path("tests/fixtures/report_v22_public_gbp/generated")
FRONTEND_DIR = Path("src/lib/report-v22/test-fixtures/public-gbp")
SCENARIOS = ("matched", "partial", "identity_conflict", "expired")


class PublicGbpFixtureInput(StrictModel):
    context: EvidenceBuildContext
    snapshot_input: CustomerPublicGbpSnapshotInput
    snapshot_id: UUID


def build_fixture(value):
    payload = build_customer_public_gbp_snapshot(value.snapshot_input)
    binding = SnapshotBinding(snapshot_id=value.snapshot_id, case_id=value.context.case_id, source_type="gbp",
                              schema_version=payload.schema_version, payload_checksum=request_digest(payload),
                              fetched_at=payload.completed_at, expires_at=payload.expires_at,
                              health_status=payload.health_status, identity_match_status=payload.identity_match_status)
    evidence = EvidenceBuildInput(context=value.context, sources=[PublicGbpEvidenceSource(binding=binding, payload=payload)])
    return build_public_findings(PublicFindingsInput(evidence_input=evidence))


def build_resources(root):
    root = root.resolve()
    directory = root / INPUT_DIR
    safe_path(root, directory)
    if {p.name for p in directory.iterdir()} != {f"{name}.json" for name in SCENARIOS}:
        raise ValueError("Unexpected public GBP input files")
    inputs, outputs, counts = {}, {}, {}
    for name in SCENARIOS:
        path = directory / f"{name}.json"
        safe_path(root, path)
        raw = path.read_bytes()
        result = build_fixture(PublicGbpFixtureInput.model_validate_json(raw))
        sample = dict(evidence_index=[item.model_dump(mode="json") for item in result.evidence_result.evidence_index],
                      findings=[item.model_dump(mode="json") for item in result.findings],
                      layers=[item.model_dump(mode="json") for item in result.site_rollup.layers])
        filename = f"{name}.json"
        inputs[filename], outputs[filename] = digest(raw), encode(sample)
        counts[filename] = {key: len(items) for key, items in sample.items()}
    manifest = dict(version=1, identity_version=IDENTITY_VERSION, synthetic_snapshots=True, inputs=inputs,
                    files={name: digest(data) for name, data in outputs.items()}, counts=counts)
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
    # Inspect every target before writing either repository. Never delete extras.
    for repository, directory in destinations:
        safe_path(repository, directory)
        if directory.exists() and (not directory.is_dir() or {p.name for p in directory.iterdir()} - set(artifacts)):
            raise ValueError("Unexpected public GBP output files")
        for name, data in artifacts.items():
            path = directory / name
            safe_path(repository, path)
            if path.exists() and not path.is_file():
                raise ValueError("Unsafe public GBP output target")
            if check and (not path.is_file() or path.read_bytes() != data):
                raise ValueError("V22_PUBLIC_GBP_FIXTURE_DRIFT: " + str(path.relative_to(repository)))
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
    except (ValueError, OSError, EvidenceError, FindingsError, PublicGbpError):
        print("Public GBP fixtures could not be generated or verified.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
