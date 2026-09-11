#!/usr/bin/env python3
"""Generate or verify deterministic full-report V2.2 golden fixtures."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
for import_root in (ROOT, TESTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from support.golden_reports import artifact_hashes, build_golden_artifacts  # noqa: E402


ACCEPTED_DIR = TESTS_ROOT / "fixtures" / "report_v22_golden"


def _write_artifacts(destination: Path, artifacts: dict[str, bytes]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, payload in artifacts.items():
        (destination / name).write_bytes(payload)


def _read_artifacts(destination: Path) -> dict[str, bytes]:
    if not destination.exists():
        return {}
    return {
        path.name: path.read_bytes()
        for path in sorted(destination.glob("*.json"))
        if path.is_file()
    }


def check_goldens(*, candidate_dir: Path | None, accept: bool) -> tuple[Path, dict[str, str]]:
    artifacts = build_golden_artifacts()
    hashes = artifact_hashes(artifacts)
    if accept:
        prepared = Path(tempfile.mkdtemp(prefix="report-v22-golden-accept-"))
        try:
            _write_artifacts(prepared, artifacts)
            ACCEPTED_DIR.mkdir(parents=True, exist_ok=True)
            for existing in ACCEPTED_DIR.glob("*.json"):
                existing.unlink()
            for candidate in prepared.glob("*.json"):
                candidate.replace(ACCEPTED_DIR / candidate.name)
        finally:
            shutil.rmtree(prepared, ignore_errors=True)
        return ACCEPTED_DIR, hashes

    destination = candidate_dir or Path(tempfile.mkdtemp(prefix="report-v22-golden-candidate-"))
    _write_artifacts(destination, artifacts)
    accepted = _read_artifacts(ACCEPTED_DIR)
    if accepted != artifacts:
        changed = sorted(set(accepted) | set(artifacts))
        changed = [name for name in changed if accepted.get(name) != artifacts.get(name)]
        raise RuntimeError(
            f"V22_REPORT_GOLDEN_DRIFT: proposed files are in {destination}; changed={changed}"
        )
    return destination, hashes


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument(
        "--accept",
        action="store_true",
        help="replace accepted fixtures; never use this option in CI",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    try:
        destination, hashes = check_goldens(
            candidate_dir=args.candidate_dir.resolve() if args.candidate_dir else None,
            accept=args.accept,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"directory": str(destination), "hashes": hashes}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
