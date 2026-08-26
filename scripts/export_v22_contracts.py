#!/usr/bin/env python3
"""Export deterministic SearchTrust v2.2 contract artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.api.v2.models import ApiV2ContractBundle
from app.report_v22.contract_version import CONTRACT_VERSION
from app.report_v22.examples import build_prospect_fixture, build_verified_fixture
from app.report_v22.models import ReportV22


DEFAULT_OUTPUT = REPO_ROOT / "contracts" / "v2.2"


class ContractExportError(RuntimeError):
    """Stable export error that is safe to display in CI logs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _schema(model: type[Any], title: str) -> dict[str, Any]:
    schema = model.model_json_schema(mode="serialization", ref_template="#/$defs/{model}")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = title
    return schema


def build_artifacts() -> dict[str, bytes]:
    fixtures = {
        "fixtures/prospect.json": build_prospect_fixture(),
        "fixtures/verified.json": build_verified_fixture(),
    }
    try:
        for fixture in fixtures.values():
            ReportV22.model_validate_json(_json_bytes(fixture))
    except ValidationError as exc:
        paths = [".".join(str(part) for part in error["loc"]) for error in exc.errors()[:10]]
        raise ContractExportError("V22_FIXTURE_INVALID", f"fixture validation failed at {paths}") from exc

    payloads: dict[str, bytes] = {
        "report_v2_2.schema.json": _json_bytes(_schema(ReportV22, "SearchTrust report_v2_2")),
        "api_v2.schema.json": _json_bytes(_schema(ApiV2ContractBundle, "SearchTrust API v2 contract bundle")),
        **{path: _json_bytes(fixture) for path, fixture in fixtures.items()},
    }
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "files": {path: _sha256(payload) for path, payload in sorted(payloads.items())},
    }
    payloads["manifest.json"] = _json_bytes(manifest)
    return payloads


def _write_tree(destination: Path, artifacts: dict[str, bytes]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for relative_path, payload in artifacts.items():
            target = temp_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return temp_root
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def _replace_tree(destination: Path, prepared: Path) -> None:
    backup = destination.with_name(f".{destination.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    moved_old = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        os.replace(prepared, destination)
    except Exception:
        if moved_old and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def _read_tree(destination: Path) -> dict[str, bytes]:
    if not destination.exists():
        return {}
    return {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }


def _check_tree(destination: Path, artifacts: dict[str, bytes]) -> None:
    existing = _read_tree(destination)
    if existing != artifacts:
        changed = sorted(set(existing) | set(artifacts))
        changed = [path for path in changed if existing.get(path) != artifacts.get(path)]
        raise ContractExportError("V22_CONTRACT_DRIFT", f"contract files differ at {changed}")


def _frontend_contract_dir(frontend_root: Path) -> Path:
    package_json = frontend_root / "package.json"
    if not package_json.is_file():
        raise ContractExportError("V22_SCHEMA_INVALID", "--frontend-dir must point to the frontend repository root")
    return frontend_root / "src" / "lib" / "report-v22" / "contracts"


def export_contracts(output: Path, frontend_root: Path | None, check: bool) -> None:
    try:
        artifacts = build_artifacts()
    except ContractExportError:
        raise
    except Exception as exc:
        raise ContractExportError("V22_SCHEMA_INVALID", "unable to generate JSON Schema") from exc

    destinations = [output]
    if frontend_root is not None:
        destinations.append(_frontend_contract_dir(frontend_root))

    if check:
        for destination in destinations:
            _check_tree(destination, artifacts)
        return

    prepared: list[tuple[Path, Path]] = []
    try:
        for destination in destinations:
            prepared.append((destination, _write_tree(destination, artifacts)))
        for destination, temp_root in prepared:
            _replace_tree(destination, temp_root)
    finally:
        for _, temp_root in prepared:
            if temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        export_contracts(args.output.resolve(), args.frontend_dir.resolve() if args.frontend_dir else None, args.check)
    except ContractExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
