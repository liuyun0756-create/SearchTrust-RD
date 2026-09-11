from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def test_retired_v21_runtime_files_are_absent() -> None:
    retired = (
        APP / "report_v21",
        APP / "api" / "v1" / "analyze.py",
        APP / "tasks" / "pipeline.py",
        APP / "tasks" / "scraper.py",
        APP / "tasks" / "address_ai.py",
        APP / "tasks" / "dify_client.py",
        APP / "core" / "task_store.py",
        APP / "models" / "request.py",
        APP / "models" / "response.py",
    )

    assert [path.relative_to(ROOT).as_posix() for path in retired if path.exists()] == []


def test_application_source_has_no_v21_or_legacy_pipeline_references() -> None:
    forbidden = (
        "report_" + "v21",
        "Report" + "V21",
        "report_" + "v2_1",
        "tasks." + "pipeline",
        "/api/v1/" + "analyze",
        "/api/v1/" + "task",
    )
    violations: list[str] = []

    for path in sorted(APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                violations.append(f"{path.relative_to(ROOT).as_posix()}: {token}")

    assert violations == []
