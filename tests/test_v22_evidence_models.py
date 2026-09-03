from uuid import UUID

import pytest
from pydantic import ValidationError

from app.report_v22.evidence_models import EvidenceBuildLimits, EvidenceSelector, MissingEvidenceSource, SnapshotBinding
from evidence_helpers import context
from test_v22_competitor_models import confirmed


def test_limits_are_bounded_and_strict():
    assert EvidenceBuildLimits().max_items == 50_000
    assert EvidenceBuildLimits().max_bytes == 20_000_000
    with pytest.raises(ValidationError):
        EvidenceBuildLimits(max_items="1")
    with pytest.raises(ValidationError):
        EvidenceBuildLimits(unexpected=True)


def test_no_snapshot_is_a_gap_not_an_invented_binding():
    missing = MissingEvidenceSource(source_type="gsc", reason="not_connected")
    assert not hasattr(missing, "snapshot_id")
    with pytest.raises(ValidationError):
        SnapshotBinding(case_id=UUID(int=1), source_type="site")


def test_selector_dimensions_must_have_unique_keys():
    with pytest.raises(ValidationError):
        EvidenceSelector(category="metric", record_key="row", field="clicks", dimensions=[
            {"key": "query", "value": "one"}, {"key": "query", "value": "two"},
        ])


@pytest.mark.parametrize("competitor_count", [1, 2, 3])
def test_evidence_context_accepts_one_to_three_competitors(competitor_count: int) -> None:
    value = context(competitors=[confirmed(index) for index in range(1, competitor_count + 1)])

    assert len(value.competitors) == competitor_count


@pytest.mark.parametrize("competitor_count", [0, 4])
def test_evidence_context_rejects_competitor_counts_outside_bounds(competitor_count: int) -> None:
    with pytest.raises(ValidationError):
        context(competitors=[confirmed(index) for index in range(1, competitor_count + 1)])
