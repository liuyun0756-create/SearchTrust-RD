"""Export synthetic offline website candidate evidence, never raw HTML to frontend."""
import argparse
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report_v22.evidence_identity import IDENTITY_VERSION
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_models import SiteBusinessFactsInput, VERSION
from scripts.export_v22_evidence_fixtures import digest, encode, safe_path

INPUT_DIR = Path("tests/fixtures/report_v22_site_business/inputs")
OUTPUT_DIR = Path("tests/fixtures/report_v22_site_business/generated")
FRONTEND_DIR = Path("src/lib/report-v22/test-fixtures/site-business")
SCENARIOS = ("structured_single", "multiple_candidates", "partial_parse", "no_content", "expired")


def build_resources(root):
    root = root.resolve()
    directory = root / INPUT_DIR
    safe_path(root, directory)
    if {p.name for p in directory.iterdir()} != {f"{name}.json" for name in SCENARIOS}:
        raise ValueError("Unexpected website business input files")
    inputs, outputs, counts = {}, {}, {}
    for name in SCENARIOS:
        path = directory / f"{name}.json"
        safe_path(root, path)
        raw = path.read_bytes()
        result = build_site_business_facts(SiteBusinessFactsInput.model_validate_json(raw))
        filename = f"{name}.json"
        inputs[filename] = digest(raw)
        outputs[filename] = encode([item.model_dump(mode="json") for item in result.evidence_index])
        counts[filename] = len(result.evidence_index)
    manifest = dict(version=1, identity_version=IDENTITY_VERSION, extraction_version=VERSION,
                    synthetic_snapshots=True, inputs=inputs, files={name: digest(data) for name, data in outputs.items()},
                    evidence_counts=counts)
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
    # Validate every target before touching either repository; never delete extras.
    for repository, directory in destinations:
        safe_path(repository, directory)
        if directory.exists() and (not directory.is_dir() or {p.name for p in directory.iterdir()} - set(artifacts)):
            raise ValueError("Unexpected website business output files")
        for name, data in artifacts.items():
            path = directory / name
            safe_path(repository, path)
            if path.exists() and not path.is_file():
                raise ValueError("Unsafe website business output target")
            if check and (not path.is_file() or path.read_bytes() != data):
                raise ValueError("V22_SITE_BUSINESS_FIXTURE_DRIFT: " + str(path.relative_to(repository)))
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
    except (ValueError, OSError, SiteBusinessError):
        print("Website business fixtures could not be generated or verified.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
