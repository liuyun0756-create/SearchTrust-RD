import json
from pathlib import Path
import shutil
import pytest

from app.report_v22.models import EvidenceItem, LayerAssessment
from app.report_v22.public_rule_catalog import GBP_RULES
from scripts.export_v22_public_gbp_fixtures import (
    build_resources, sync_resources, build_fixture, PublicGbpFixtureInput,
    INPUT_DIR, OUTPUT_DIR, FRONTEND_DIR, SCENARIOS,
)

ROOT = Path(__file__).resolve().parents[1]


def setup_repos(tmp_path):
    backend, frontend = tmp_path / "backend", tmp_path / "frontend"
    shutil.copytree(ROOT / INPUT_DIR, backend / INPUT_DIR)
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    return backend, frontend


def test_real_builder_samples_match_independent_coverage_expectations():
    resources = build_resources(ROOT)
    for name in SCENARIOS:
        sample = json.loads(resources[f"{name}.json"])
        assert set(sample) == {"findings", "evidence_index", "layers"}
        assert sample["findings"] == []
        assert len(sample["evidence_index"]) == (1 if name in {"expired", "identity_conflict"} else 10 if name == "matched" else 9)
        for item in sample["evidence_index"]:
            EvidenceItem.model_validate_json(json.dumps(item))
        assert len(sample["layers"]) == 8
        for item in sample["layers"]:
            LayerAssessment.model_validate_json(json.dumps(item))
            assert item["status"] == "not_checked" and not item["finding_ids"] and not item["evidence_ids"]
        result = build_fixture(PublicGbpFixtureInput.model_validate_json((ROOT / INPUT_DIR / f"{name}.json").read_bytes()))
        checks = [e for e in result.rule_evaluations if e.rule_id in GBP_RULES]
        ids = {e.evidence_id for e in result.evidence_result.evidence_index}
        assert len(checks) == 4
        assert all(e.state == "not_checked" and e.evidence_ids and set(e.evidence_ids) <= ids and not e.comparator_ids for e in checks)
        if name in {"expired", "identity_conflict"}:
            assert sample["evidence_index"][0]["normalized_value"] == ("expired" if name == "expired" else "identity_mismatch")
            assert all(e.reason == "source_ineligible" for e in checks)
        elif name == "partial":
            assert [e.reason for e in checks].count("field_not_observed") == 3
            assert sorted(e.normalized_value for e in result.evidence_result.evidence_index if e.source_type == "coverage") == ["empty", "partial", "partial"]
        else:
            assert all(e.reason == "gbp_alignment_not_implemented" for e in checks)


def test_sync_and_check_are_stable_and_read_only(tmp_path):
    backend, frontend = setup_repos(tmp_path)
    sync_resources(backend, frontend)
    before = {p: p.stat().st_mtime_ns for root in (backend, frontend) for p in root.rglob("*") if p.is_file()}
    sync_resources(backend, frontend, check=True)
    sync_resources(backend, frontend)
    assert before == {p: p.stat().st_mtime_ns for p in before}
    for p in (backend / OUTPUT_DIR).iterdir():
        assert p.read_bytes() == (frontend / FRONTEND_DIR / p.name).read_bytes()
    target = frontend / FRONTEND_DIR / "matched.json"
    target.write_text("[]\n")
    with pytest.raises(ValueError, match="DRIFT"):
        sync_resources(backend, frontend, check=True)
    assert target.read_text() == "[]\n"


@pytest.mark.parametrize("location", ["input", "backend_output", "frontend_output"])
def test_all_paths_preflight_and_unrelated_files_survive(tmp_path, location):
    backend, frontend = setup_repos(tmp_path)
    parent = {"input": backend / INPUT_DIR, "backend_output": backend / OUTPUT_DIR, "frontend_output": frontend / FRONTEND_DIR}[location]
    parent.mkdir(parents=True, exist_ok=True)
    unexpected = parent / "user-note.txt"
    unexpected.write_text("keep me")
    with pytest.raises(ValueError, match="Unexpected"):
        sync_resources(backend, frontend)
    assert not (backend / OUTPUT_DIR / "matched.json").exists()
    assert unexpected.read_text() == "keep me"


@pytest.mark.parametrize("target_kind", ["directory", "input", "output", "internal_link"])
def test_symlinks_are_rejected_before_writes(tmp_path, target_kind):
    backend, frontend = setup_repos(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = frontend / FRONTEND_DIR
    if target_kind == "directory":
        parent.parent.mkdir(parents=True)
        parent.symlink_to(outside, target_is_directory=True)
    elif target_kind == "output":
        parent.mkdir(parents=True)
        (parent / "matched.json").symlink_to(outside / "target.json")
    elif target_kind == "internal_link":
        parent.mkdir(parents=True)
        (parent / "matched.json").symlink_to(frontend / "package.json")
    else:
        source = backend / INPUT_DIR / "matched.json"
        source.rename(outside / "target.json")
        source.symlink_to(outside / "target.json")
    before = {p: p.read_bytes() for p in outside.iterdir()}
    with pytest.raises(ValueError, match="Unsafe"):
        sync_resources(backend, frontend)
    assert before == {p: p.read_bytes() for p in outside.iterdir()}
    assert not (backend / OUTPUT_DIR).exists()


def test_check_never_creates_missing_directories(tmp_path):
    backend, frontend = setup_repos(tmp_path)
    with pytest.raises(ValueError, match="DRIFT"):
        sync_resources(backend, frontend, check=True)
    assert not (backend / OUTPUT_DIR).exists() and not (frontend / FRONTEND_DIR).exists()
