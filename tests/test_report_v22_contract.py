from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.report_v22.models import ReportV22
from scripts.export_v22_contracts import build_artifacts


CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts" / "v2.2"


def load_fixture(name: str) -> dict:
    return json.loads((CONTRACT_DIR / "fixtures" / name).read_text(encoding="utf-8"))


def validate(payload: dict) -> ReportV22:
    return ReportV22.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("name", ["prospect.json", "verified.json"])
def test_shared_fixtures_validate(name: str) -> None:
    report = ReportV22.model_validate_json((CONTRACT_DIR / "fixtures" / name).read_text(encoding="utf-8"))
    assert report.report_version.schema_version == "2.2.0"
    assert [action.sequence for action in report.top_actions] == [1, 2, 3]


def test_unknown_fields_are_rejected() -> None:
    payload = load_fixture("prospect.json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validate(payload)


def test_top_actions_must_contain_exactly_three_items() -> None:
    payload = load_fixture("prospect.json")
    payload["top_actions"].pop()
    with pytest.raises(ValidationError, match="at least 3 items"):
        validate(payload)


def test_top_action_sequences_must_be_ordered() -> None:
    payload = load_fixture("prospect.json")
    payload["top_actions"][1]["sequence"] = 3
    with pytest.raises(ValidationError, match="sequences 1, 2, 3"):
        validate(payload)


def test_duplicate_evidence_ids_are_rejected() -> None:
    payload = load_fixture("prospect.json")
    duplicate = copy.deepcopy(payload["evidence_index"][0])
    payload["evidence_index"].append(duplicate)
    with pytest.raises(ValidationError, match="duplicate evidence IDs"):
        validate(payload)


def test_dangling_evidence_references_are_rejected() -> None:
    payload = load_fixture("prospect.json")
    payload["findings"][0]["evidence_ids"] = ["ev_missing_reference"]
    with pytest.raises(ValidationError, match="unknown evidence references"):
        validate(payload)


def test_dangling_finding_references_are_rejected() -> None:
    payload = load_fixture("prospect.json")
    payload["top_actions"][0]["finding_ids"] = ["fn_missing_reference"]
    with pytest.raises(ValidationError, match="unknown finding references"):
        validate(payload)


def test_prospect_cannot_contain_first_party_snapshot() -> None:
    payload = load_fixture("prospect.json")
    payload["first_party_performance"]["gsc"]["connection_state"] = "verified"
    payload["first_party_performance"]["gsc"]["snapshot_id"] = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    with pytest.raises(ValidationError, match="prospect reports must not contain first-party snapshots"):
        validate(payload)


def test_verified_requires_parent_report() -> None:
    payload = load_fixture("verified.json")
    payload["report_version"]["parent_report_id"] = None
    with pytest.raises(ValidationError, match="verified reports require a parent report"):
        validate(payload)


def test_verified_requires_all_three_sync_snapshots() -> None:
    payload = load_fixture("verified.json")
    payload["first_party_performance"]["ga4"]["snapshot_id"] = None
    with pytest.raises(ValidationError, match="require GSC, GBP, and GA4 sync snapshots"):
        validate(payload)


def test_full_coverage_requires_healthy_matched_sources() -> None:
    payload = load_fixture("verified.json")
    payload["first_party_performance"]["gbp"]["health_status"] = "unhealthy"
    with pytest.raises(ValidationError, match="full evidence coverage requires healthy"):
        validate(payload)


def test_previous_finding_must_point_to_parent_report() -> None:
    payload = load_fixture("verified.json")
    payload["version_diff"]["entries"][0]["previous_finding"]["report_id"] = "44444444-4444-4444-8444-444444444444"
    with pytest.raises(ValidationError, match="previous finding references must point to the parent report"):
        validate(payload)


def test_eight_layers_are_canonical_and_ordered() -> None:
    payload = load_fixture("prospect.json")
    payload["eight_layers"][0], payload["eight_layers"][1] = payload["eight_layers"][1], payload["eight_layers"][0]
    with pytest.raises(ValidationError, match="canonical ordered layer keys"):
        validate(payload)


def test_contract_generation_is_deterministic() -> None:
    assert build_artifacts() == build_artifacts()


def test_manifest_hashes_match_contract_files() -> None:
    manifest = json.loads((CONTRACT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["contract_version"] == "2.2.0"
    for relative_path, expected_hash in manifest["files"].items():
        actual_hash = "sha256:" + hashlib.sha256((CONTRACT_DIR / relative_path).read_bytes()).hexdigest()
        assert actual_hash == expected_hash
