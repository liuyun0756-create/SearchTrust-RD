import json
from pathlib import Path
import shutil

import pytest

from app.report_v22.models import EvidenceItem
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_models import SiteBusinessFactsInput
from scripts.export_v22_site_business_fixtures import (
    build_resources, sync_resources, INPUT_DIR, OUTPUT_DIR, FRONTEND_DIR, SCENARIOS,
)

ROOT = Path(__file__).resolve().parents[1]


def setup_repos(tmp_path):
    backend, frontend = tmp_path / "backend", tmp_path / "frontend"
    shutil.copytree(ROOT / INPUT_DIR, backend / INPUT_DIR)
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    return backend, frontend


@pytest.mark.parametrize("name,count", [("structured_single", 7), ("multiple_candidates", 30), ("partial_parse", 2), ("no_content", 0), ("expired", 0)])
def test_fixture_evidence_and_candidate_semantics(name, count):
    resources = build_resources(ROOT)
    sample = json.loads(resources[f"{name}.json"])
    assert isinstance(sample, list) and len(sample) == count
    for item in sample:
        EvidenceItem.model_validate_json(json.dumps(item))
        assert item["source_type"] == "site" and item["confidence"] == "low"
        assert item["original_value"] == item["normalized_value"]
    result = build_site_business_facts(SiteBusinessFactsInput.model_validate_json((ROOT / INPUT_DIR / f"{name}.json").read_bytes()))
    assert [i.model_dump(mode="json") for i in result.evidence_index] == sample
    if name == "multiple_candidates":
        assert len(result.candidates) == 26 and len(result.pages) == 2
        assert all(p.fields["phone"].ownership_status == "mixed" for p in result.pages)
    elif name == "partial_parse":
        assert result.pages[0].status == "partial"
        assert result.pages[0].fields["business_name"].observation_status == "not_checked"
        assert result.pages[0].fields["phone"].observation_status == "observed"
    elif name == "no_content":
        assert result.pages[0].diagnostics[0].code == "html_missing"
    elif name == "expired":
        assert result.source_status.reason == "source_expired" and result.pages == []


def test_sync_and_check_are_stable_and_read_only(tmp_path):
    backend, frontend = setup_repos(tmp_path)
    sync_resources(backend, frontend)
    before = {p: p.stat().st_mtime_ns for root in (backend, frontend) for p in root.rglob("*") if p.is_file()}
    sync_resources(backend, frontend, check=True)
    sync_resources(backend, frontend)
    assert before == {p: p.stat().st_mtime_ns for p in before}
    for p in (backend / OUTPUT_DIR).iterdir():
        assert p.read_bytes() == (frontend / FRONTEND_DIR / p.name).read_bytes()
    target = frontend / FRONTEND_DIR / "structured_single.json"
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
    assert not (backend / OUTPUT_DIR / "structured_single.json").exists()
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
        (parent / "structured_single.json").symlink_to(outside / "target.json")
    elif target_kind == "internal_link":
        parent.mkdir(parents=True)
        (parent / "structured_single.json").symlink_to(frontend / "package.json")
    else:
        source = backend / INPUT_DIR / "structured_single.json"
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


def test_unusable_frontend_does_not_allow_backend_writes(tmp_path):
    backend, frontend = setup_repos(tmp_path)
    with pytest.raises(ValueError, match="separate frontend"):
        sync_resources(backend, backend)
    assert not (backend / OUTPUT_DIR).exists()


def test_output_directory_as_file_is_rejected_before_writes(tmp_path):
    backend, frontend = setup_repos(tmp_path)
    target = frontend / FRONTEND_DIR
    target.parent.mkdir(parents=True)
    target.write_text("user data")
    with pytest.raises(ValueError, match="Unexpected"):
        sync_resources(backend, frontend)
    assert target.read_text() == "user data" and not (backend / OUTPUT_DIR).exists()
