import json
from pathlib import Path
import shutil
import pytest

from scripts.export_v22_evidence_fixtures import build_resources, sync_resources, INPUT_DIR, OUTPUT_DIR, FRONTEND_DIR

ROOT=Path(__file__).resolve().parents[1]


def setup_repos(tmp_path):
    backend=tmp_path/"backend"
    frontend=tmp_path/"frontend"
    shutil.copytree(ROOT/INPUT_DIR,backend/INPUT_DIR)
    frontend.mkdir()
    (frontend/"package.json").write_text("{}")
    return backend,frontend


def test_generated_outputs_have_independent_semantic_expectations():
    artifacts=build_resources(ROOT)
    verified=json.loads(artifacts["verified.json"])
    assert len(verified)==6
    assert {e["source_type"] for e in verified}=={"gsc","gbp","ga4"}
    assert sum(e["normalized_value"]==0 for e in verified)==3
    gaps=json.loads(artifacts["gaps.json"])
    assert len(gaps)==1 and gaps[0]["source_type"]=="coverage"
    assert gaps[0]["normalized_value"]=="unhealthy"
    public=json.loads(artifacts["public.json"])
    assert {e["source_type"] for e in public}=={"site","serp","competitor","coverage"}
    assert any(e["original_value"]=="We repair plumbing systems for homes in Austin." for e in public)


def test_sync_check_is_byte_identical_and_read_only_and_detects_drift(tmp_path):
    backend,frontend=setup_repos(tmp_path)
    sync_resources(backend,frontend)
    before={p:p.stat().st_mtime_ns for root in (backend,frontend) for p in root.rglob("*") if p.is_file()}
    sync_resources(backend,frontend,check=True)
    assert before=={p:p.stat().st_mtime_ns for p in before}
    for p in (backend/OUTPUT_DIR).iterdir():
        assert p.read_bytes()==(frontend/FRONTEND_DIR/p.name).read_bytes()
    target=frontend/FRONTEND_DIR/"public.json"
    target.write_text("[]\n")
    with pytest.raises(ValueError,match="DRIFT"):
        sync_resources(backend,frontend,check=True)
    assert target.read_text()=="[]\n"


def test_all_destinations_preflight_before_writes_and_unrelated_files_survive(tmp_path):
    backend,frontend=setup_repos(tmp_path)
    unexpected=frontend/FRONTEND_DIR/"user-note.txt"
    unexpected.parent.mkdir(parents=True)
    unexpected.write_text("keep me")
    with pytest.raises(ValueError,match="Unexpected"):
        sync_resources(backend,frontend)
    assert not (backend/OUTPUT_DIR).exists()
    assert unexpected.read_text()=="keep me"


def test_rejects_symlink_destination_without_touching_target(tmp_path):
    backend,frontend=setup_repos(tmp_path)
    outside=tmp_path/"outside"
    outside.mkdir()
    parent=frontend/FRONTEND_DIR
    parent.parent.mkdir(parents=True)
    parent.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match="Unsafe"):
        sync_resources(backend,frontend)
    assert not list(outside.iterdir())
