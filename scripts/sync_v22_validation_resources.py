"""Generate/check auxiliary validation resources without changing frozen contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unicodedata

from pydantic import ConfigDict, TypeAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.v22_validation_cases import load_cases

RESOURCE_DIR = Path("tests/fixtures/report_v22_validation")
NORMALIZATION_FILE = Path("app/report_v22/normalization_data.json")
FRONTEND_RESOURCES = Path("src/lib/report-v22/test-fixtures/validation")
FRONTEND_NORMALIZATION = Path("src/lib/report-v22/generated/normalization-data.json")


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def normalization_data() -> dict:
    strip_adapter = TypeAdapter(str, config=ConfigDict(str_strip_whitespace=True))
    casefold = {}
    python_whitespace = []
    string_whitespace = []
    for codepoint in range(sys.maxunicode + 1):
        char = chr(codepoint)
        folded = char.casefold()
        if char != folded:
            casefold[char] = folded
        if char.isspace():
            python_whitespace.append(char)
            if strip_adapter.validate_python(char) == "":
                string_whitespace.append(char)
    return {
        "unicode_version": unicodedata.unidata_version,
        "casefold": casefold,
        "python_whitespace": python_whitespace,
        "string_whitespace": string_whitespace,
    }


def build_resources(root: Path) -> dict[str, bytes]:
    cases = (root / RESOURCE_DIR / "cases.json").read_bytes()
    fixtures = {name: (root / "contracts/v2.2/fixtures" / f"{name}.json").read_bytes() for name in ("prospect", "verified")}
    load_cases(json.loads(cases), {name: json.loads(payload) for name, payload in fixtures.items()})
    normalization = json_bytes(normalization_data())
    manifest = json_bytes({
        "version": 1,
        "files": {"cases.json": digest(cases), "normalization-data.json": digest(normalization)},
        "fixtures": {name: digest(payload) for name, payload in fixtures.items()},
    })
    return {"cases.json": cases, "normalization-data.json": normalization, "manifest.json": manifest}


def resource_paths(root: Path, frontend: bool = False) -> dict[str, Path]:
    directory = FRONTEND_RESOURCES if frontend else RESOURCE_DIR
    return {
        "cases.json": root / directory / "cases.json",
        "manifest.json": root / directory / "manifest.json",
        "normalization-data.json": root / (FRONTEND_NORMALIZATION if frontend else NORMALIZATION_FILE),
    }


def sync_resources(root: Path, frontend: Path | None = None, *, check: bool = False) -> None:
    root = root.resolve()
    artifacts = build_resources(root)
    destinations = [(root, resource_paths(root))]
    if frontend is not None:
        frontend = frontend.resolve()
        if frontend == root or not (frontend / "package.json").is_file():
            raise ValueError("Frontend directory must be a separate frontend repository")
        destinations.append((frontend, resource_paths(frontend, True)))

    # Validate every destination before writing any of them. Never replace a directory.
    for destination, paths in destinations:
        for path in paths.values():
            if not path.resolve().is_relative_to(destination) or path.is_symlink():
                raise ValueError("Validation resource path escapes its repository")
        directory = paths["cases.json"].parent
        if directory.exists() and {p.name for p in directory.iterdir()} - {"cases.json", "manifest.json"}:
            raise ValueError("Unexpected files in validation resource directory")
        if check:
            for name, path in paths.items():
                if not path.is_file() or path.read_bytes() != artifacts[name]:
                    raise ValueError(f"V22_VALIDATION_RESOURCE_DRIFT: {path.relative_to(destination)}")
    if check:
        return
    for _, paths in destinations:
        for name, path in paths.items():
            if path.is_file() and path.read_bytes() == artifacts[name]:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as temporary:
                temporary.write(artifacts[name])
                prepared = Path(temporary.name)
            try:
                os.replace(prepared, path)
            finally:
                prepared.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        sync_resources(REPO_ROOT, args.frontend_dir, check=args.check)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
