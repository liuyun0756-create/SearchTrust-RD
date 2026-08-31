import json
from pathlib import Path
import shutil
import pytest

from app.report_v22.findings import build_public_findings
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.models import EvidenceItem, Finding, LayerAssessment, REQUIRED_LAYER_KEYS
from app.report_v22.public_rule_catalog import RULES
from scripts.export_v22_findings_fixtures import build_resources, sync_resources, INPUT_DIR, OUTPUT_DIR, FRONTEND_DIR

ROOT = Path(__file__).resolve().parents[1]


def setup_repos(tmp_path):
    backend, frontend = tmp_path / "backend", tmp_path / "frontend"
    shutil.copytree(ROOT / INPUT_DIR, backend / INPUT_DIR)
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    return backend, frontend


def test_generated_samples_have_independent_semantic_expectations():
    artifacts = build_resources(ROOT)
    triggered = json.loads(artifacts["triggered.json"])
    assert len(triggered["findings"]) == 7
    assert {item["rule_id"] for item in triggered["findings"]} == set(RULES)
    clear = json.loads(artifacts["clear.json"])
    assert clear["findings"] == [] and clear["evidence_index"]
    gaps = json.loads(artifacts["gaps.json"])
    assert gaps["findings"] == []
    assert len(gaps["evidence_index"]) == 1
    assert gaps["evidence_index"][0]["source_type"] == "coverage"
    assert gaps["evidence_index"][0]["normalized_value"] == "expired"
    for sample in (triggered, clear, gaps):
        ids = {e["evidence_id"] for e in sample["evidence_index"]}
        for item in sample["evidence_index"]:
            EvidenceItem.model_validate_json(json.dumps(item))
        for item in sample["findings"]:
            Finding.model_validate_json(json.dumps(item))
            assert set(item["evidence_ids"] + item["comparator_ids"]) <= ids
            assert item["confidence"] in {"low", "medium"}
            assert item["missing_data"] and item["change_conditions"]
        for item in sample["layers"]:
            LayerAssessment.model_validate_json(json.dumps(item))
            assert item["status"] == "not_checked" and not item["evidence_ids"] and not item["finding_ids"]
        assert [item["layer_key"] for item in sample["layers"][:8]] == list(REQUIRED_LAYER_KEYS)
    # A sample without Findings is not a semantic all-clear report.
    for name in ("clear", "gaps"):
        value = PublicFindingsInput.model_validate_json((ROOT / INPUT_DIR / f"{name}.json").read_bytes())
        result = build_public_findings(value)
        assert all(layer.status == "not_checked" for layer in result.site_rollup.layers)
        if name == "gaps":
            assert result.site_rollup.counts.checked is None
            assert all(e.state == "not_checked" for e in result.rule_evaluations)


def test_sync_check_is_identical_read_only_and_detects_drift(tmp_path):
    backend, frontend = setup_repos(tmp_path)
    sync_resources(backend, frontend)
    before = {p: p.stat().st_mtime_ns for root in (backend, frontend) for p in root.rglob("*") if p.is_file()}
    sync_resources(backend, frontend, check=True)
    assert before == {p: p.stat().st_mtime_ns for p in before}
    for p in (backend / OUTPUT_DIR).iterdir():
        assert p.read_bytes() == (frontend / FRONTEND_DIR / p.name).read_bytes()
    target = frontend / FRONTEND_DIR / "triggered.json"
    target.write_text("[]\n")
    with pytest.raises(ValueError, match="DRIFT"):
        sync_resources(backend, frontend, check=True)
    assert target.read_text() == "[]\n"


def test_all_destinations_preflight_and_unrelated_files_survive(tmp_path):
    backend, frontend = setup_repos(tmp_path)
    unexpected = frontend / FRONTEND_DIR / "user-note.txt"
    unexpected.parent.mkdir(parents=True)
    unexpected.write_text("keep me")
    with pytest.raises(ValueError, match="Unexpected"):
        sync_resources(backend, frontend)
    assert not (backend / OUTPUT_DIR).exists()
    assert unexpected.read_text() == "keep me"


@pytest.mark.parametrize("target_kind", ["directory", "input", "output"])
def test_symlinks_are_rejected_without_touching_targets(tmp_path, target_kind):
    backend, frontend = setup_repos(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = frontend / FRONTEND_DIR
    if target_kind == "directory":
        parent.parent.mkdir(parents=True)
        parent.symlink_to(outside, target_is_directory=True)
    elif target_kind == "output":
        parent.mkdir(parents=True)
        (parent / "triggered.json").symlink_to(outside / "target.json")
    else:
        source = backend / INPUT_DIR / "triggered.json"
        source.rename(outside / "target.json")
        source.symlink_to(outside / "target.json")
    before = {p: p.read_bytes() for p in outside.iterdir()}
    with pytest.raises(ValueError, match="Unsafe"):
        sync_resources(backend, frontend)
    assert before == {p: p.read_bytes() for p in outside.iterdir()}
    assert not (backend / OUTPUT_DIR).exists()


def test_check_never_creates_missing_output_directories(tmp_path):
    backend, frontend = setup_repos(tmp_path)
    with pytest.raises(ValueError, match="DRIFT"):
        sync_resources(backend, frontend, check=True)
    assert not (backend / OUTPUT_DIR).exists()
    assert not (frontend / FRONTEND_DIR).exists()
