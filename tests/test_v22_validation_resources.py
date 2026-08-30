from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.sync_v22_validation_resources import (
    FRONTEND_NORMALIZATION, FRONTEND_RESOURCES, NORMALIZATION_FILE, REPO_ROOT,
    RESOURCE_DIR, build_resources, normalization_data, resource_paths, sync_resources,
)


def test_committed_resources_are_reproducible() -> None:
    sync_resources(REPO_ROOT, check=True)
    assert build_resources(REPO_ROOT) == build_resources(REPO_ROOT)


def test_normalization_table_matches_backend() -> None:
    table = normalization_data()
    for value in ["Straße", "STRASSE", "ΟΣ", "οσ", "ος", "I", "ı", "İ", "𐐀"]:
        assert "".join(table["casefold"].get(char, char) for char in value) == value.casefold()
    assert "\u001c" in table["python_whitespace"]
    assert "\ufeff" not in table["python_whitespace"]


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path]:
    backend, frontend = tmp_path / "backend", tmp_path / "frontend"
    (backend / RESOURCE_DIR).mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / RESOURCE_DIR / "cases.json", backend / RESOURCE_DIR / "cases.json")
    shutil.copytree(REPO_ROOT / "contracts", backend / "contracts")
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    return backend, frontend


def test_sync_and_check_preserve_unrelated_files(repositories: tuple[Path, Path]) -> None:
    backend, frontend = repositories
    sibling = frontend / FRONTEND_NORMALIZATION.parent / "types.ts"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("keep")
    frozen = {path: path.read_bytes() for path in (backend / "contracts").rglob("*") if path.is_file()}
    sync_resources(backend, frontend)
    paths = [*resource_paths(backend).values(), *resource_paths(frontend, True).values()]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    sync_resources(backend, frontend, check=True)
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    assert sibling.read_text() == "keep"
    assert all(path.read_bytes() == content for path, content in frozen.items())


@pytest.mark.parametrize("target", ["cases.json", "manifest.json", "normalization-data.json"])
def test_check_detects_drift_without_repair(repositories: tuple[Path, Path], target: str) -> None:
    backend, frontend = repositories
    sync_resources(backend, frontend)
    path = resource_paths(frontend, True)[target]
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="DRIFT"):
        sync_resources(backend, frontend, check=True)
    assert path.read_bytes() == b"changed"


def test_missing_resources_check_does_not_create_files(repositories: tuple[Path, Path]) -> None:
    backend, frontend = repositories
    with pytest.raises(ValueError, match="DRIFT"):
        sync_resources(backend, frontend, check=True)
    assert not (backend / NORMALIZATION_FILE).exists()
    assert not (frontend / FRONTEND_RESOURCES).exists()


def test_invalid_destination_fails_before_backend_write(repositories: tuple[Path, Path]) -> None:
    backend, frontend = repositories
    with pytest.raises(ValueError):
        sync_resources(backend, frontend / "missing")
    assert not (backend / NORMALIZATION_FILE).exists()


def test_symlink_escape_is_rejected(repositories: tuple[Path, Path], tmp_path: Path) -> None:
    backend, frontend = repositories
    outside = tmp_path / "outside"
    outside.mkdir()
    (frontend / "src").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        sync_resources(backend, frontend)
    assert list(outside.iterdir()) == []
    assert not (backend / NORMALIZATION_FILE).exists()


def test_unknown_resource_is_not_removed(repositories: tuple[Path, Path]) -> None:
    backend, frontend = repositories
    directory = frontend / FRONTEND_RESOURCES
    directory.mkdir(parents=True)
    extra = directory / "user-note.txt"
    extra.write_text("keep")
    with pytest.raises(ValueError, match="Unexpected"):
        sync_resources(backend, frontend)
    assert extra.read_text() == "keep"
