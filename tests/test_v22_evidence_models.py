from uuid import UUID

import pytest
from pydantic import ValidationError

from app.report_v22.evidence_models import EvidenceBuildLimits, EvidenceSelector, MissingEvidenceSource, SnapshotBinding


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
