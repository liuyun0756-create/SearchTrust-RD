from pathlib import Path
from urllib.parse import urlparse
import json
import re

import pytest


pytestmark = pytest.mark.contract

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "provider_contracts"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"
REAL_CREDENTIAL_PATTERN = re.compile(
    r"(?:Bearer\s+|eyJ[A-Za-z0-9_-]{20,}\.|(?:sk|rk|pk)_[A-Za-z0-9_-]{12,})",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://[^\s\"']+")


def test_manifest_covers_every_external_provider_boundary() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = manifest["operations"]

    assert manifest["schema_version"] == "v22_provider_contract_manifest_v1"
    assert {entry["provider"] for entry in entries} == {
        "serpapi",
        "firecrawl",
        "dify",
        "google",
        "delivery",
    }
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert all(entry["adapter"].startswith("app.") for entry in entries)
    assert all(entry["request_contract"] for entry in entries)
    assert all(entry["error_mapping"] for entry in entries)
    assert all(0 <= entry["max_response_bytes"] <= 2_000_000 for entry in entries)


def test_manifested_fixtures_are_json_sanitized_and_offline_only() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    declared_paths: list[str] = []
    for entry in manifest["operations"]:
        for relative_path in entry["fixtures"]:
            declared_paths.append(relative_path)
            path = FIXTURE_ROOT / relative_path
            assert path.is_file(), entry["id"]
            raw = path.read_text(encoding="utf-8")
            json.loads(raw)
            assert not REAL_CREDENTIAL_PATTERN.search(raw), entry["id"]
            for url in URL_PATTERN.findall(raw):
                assert urlparse(url).hostname == "fixture.example", entry["id"]
    assert len(declared_paths) == len(set(declared_paths))
