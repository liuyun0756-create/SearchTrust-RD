from __future__ import annotations

from pathlib import Path

import pytest

from support.golden_reports import (
    GOLDEN_NAMES,
    GoldenReportError,
    assert_golden_semantics,
    build_golden_artifacts,
    normalize_payload,
)


GOLDEN_DIR = Path(__file__).parent / "fixtures" / "report_v22_golden"


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_full_report_golden(name: str) -> None:
    expected = (GOLDEN_DIR / f"{name}.json").read_bytes()
    actual = build_golden_artifacts()[f"{name}.json"]
    assert actual == expected


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_semantics_are_independently_valid(name: str) -> None:
    import json

    payload = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert_golden_semantics(name, payload)


def test_normalization_rejects_every_undeclared_dynamic_path() -> None:
    with pytest.raises(GoldenReportError, match="undeclared"):
        normalize_payload(
            "prospect",
            {"report_version": {"generated_at": "2026-09-08T00:00:00Z"}},
            replacements={"report_version.generated_at": "<generated-at>"},
        )
