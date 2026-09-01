import json
from pathlib import Path
import shutil

import pytest

from app.report_v22.findings_models import PublicFindingsResult
from app.report_v22.public_rule_catalog import GBP_RULES
from scripts.export_v22_site_gbp_alignment_fixtures import (
    FRONTEND_DIR,
    GBP_BASE,
    INPUT_DIR,
    OUTPUT_DIR,
    SCENARIOS,
    SITE_BASE,
    build_resources,
    sync_resources,
)

ROOT = Path(__file__).resolve().parents[1]


def setup_repositories(tmp_path):
    backend, frontend = tmp_path / "backend", tmp_path / "frontend"
    shutil.copytree(ROOT / INPUT_DIR, backend / INPUT_DIR)
    for source in (SITE_BASE, GBP_BASE):
        target = backend / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, target)
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    return backend, frontend


def test_all_fixed_scenarios_are_strict_v110_results_with_declared_expectations():
    resources = build_resources(ROOT)
    manifest = json.loads(resources["manifest.json"])
    assert manifest["rule_version"] == "1.1.0"
    assert set(manifest["files"]) == {f"{name}.json" for name in SCENARIOS}
    for scenario in SCENARIOS:
        filename = f"{scenario}.json"
        result = PublicFindingsResult.model_validate_json(resources[filename])
        evaluations = [item for item in result.rule_evaluations if item.rule_id in GBP_RULES]
        assert len(evaluations) == 4
        assert {item.rule_version for item in evaluations} == {"1.1.0"}
        assert manifest["states"][filename] == {
            item.rule_id: {"state": item.state, "reason": item.reason} for item in evaluations
        }
        assert manifest["finding_counts"][filename] == sum(item.rule_id in GBP_RULES for item in result.findings)
        assert result.model_dump(mode="json") == json.loads(resources[filename])


def test_scenario_semantics_cover_every_approved_branch():
    manifest = json.loads(build_resources(ROOT)["manifest.json"])
    states = manifest["states"]
    field = lambda scenario, rule: states[f"{scenario}.json"][f"v22_public.gbp_{rule}_alignment"]
    assert {item["reason"] for item in states["exact_match.json"].values()} == {"exact_match"}
    assert {item["reason"] for item in states["semantic_match.json"].values()} == {"semantic_match", "exact_match"}
    assert field("site_missing", "phone")["reason"] == "site_field_missing"
    assert field("gbp_missing", "phone")["reason"] == "gbp_field_missing"
    assert field("both_missing", "phone")["reason"] == "both_fields_missing"
    assert field("service_area_partial", "service_area")["reason"] == "partial_match"
    assert field("service_area_mismatch", "service_area")["reason"] == "value_mismatch"
    assert field("operating_model_not_applicable", "service_area")["reason"] == "field_not_applicable"
    assert field("identity_unresolved", "phone")["reason"] == "identity_unresolved"
    assert {item["reason"] for item in states["source_unavailable.json"].values()} == {"source_ineligible"}
    assert {item["reason"] for item in states["comparison_time_gap.json"].values()} == {"comparison_time_gap"}


def test_export_sync_is_identical_and_check_only(tmp_path):
    backend, frontend = setup_repositories(tmp_path)
    sync_resources(backend, frontend)
    before = {path: path.stat().st_mtime_ns for root in (backend, frontend) for path in root.rglob("*") if path.is_file()}
    sync_resources(backend, frontend, check=True)
    sync_resources(backend, frontend)
    assert before == {path: path.stat().st_mtime_ns for path in before}
    for path in (backend / OUTPUT_DIR).iterdir():
        assert path.read_bytes() == (frontend / FRONTEND_DIR / path.name).read_bytes()
    target = frontend / FRONTEND_DIR / "exact_match.json"
    target.write_text("{}\n")
    with pytest.raises(ValueError, match="DRIFT"):
        sync_resources(backend, frontend, check=True)
    assert target.read_text() == "{}\n"


def test_unexpected_output_is_preserved_and_blocks_all_writes(tmp_path):
    backend, frontend = setup_repositories(tmp_path)
    target = frontend / FRONTEND_DIR
    target.mkdir(parents=True)
    note = target / "user-note.txt"
    note.write_text("keep")
    with pytest.raises(ValueError, match="Unexpected"):
        sync_resources(backend, frontend)
    assert note.read_text() == "keep" and not (backend / OUTPUT_DIR).exists()
