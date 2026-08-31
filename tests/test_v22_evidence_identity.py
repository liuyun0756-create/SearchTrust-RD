from datetime import datetime, timezone
from uuid import UUID

import pytest
import subprocess
import sys

from app.report_v22.evidence_identity import evidence_id
from app.report_v22.evidence_models import EvidenceObservation, EvidenceSelector
from app.report_v22.models import SourceLocator


def observation(value=1, **changes):
    values = dict(
        snapshot_id=UUID("11111111-1111-4111-8111-111111111111"),
        source_type="site",
        selector=EvidenceSelector(category="site_field", record_key="page", field="status_code"),
        source_locator=SourceLocator(url="https://example.test/"),
        original_value=value, normalized_value=value,
        collected_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        confidence="medium", health_status="healthy",
        origin_paths=["/payload/pages/0/status_code"],
    )
    values.update(changes)
    return EvidenceObservation(**values)


def test_identity_is_stable_and_contract_shaped():
    first = evidence_id(observation())
    assert first == evidence_id(observation())
    assert first.startswith("ev_") and len(first) == 67


def test_identity_distinguishes_scalar_types_and_snapshots():
    assert len({evidence_id(observation(value)) for value in [True, 1, 1.0, "1", None]}) == 5
    assert evidence_id(observation()) != evidence_id(observation(snapshot_id=UUID(int=2)))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_are_rejected(value):
    with pytest.raises(ValueError):
        observation(value)


def test_identity_survives_a_fresh_process_and_sorted_dimensions():
    original = observation(selector=EvidenceSelector(category="metric",record_key="row",field="clicks",dimensions=[{"key":"query","value":"plumber"},{"key":"device","value":"mobile"}]))
    reverse = original.model_copy(deep=True)
    reverse.selector = EvidenceSelector.model_validate({**original.selector.model_dump(mode="python"),"dimensions":list(reversed(original.selector.dimensions))})
    assert evidence_id(original)==evidence_id(reverse)
    code="from app.report_v22.evidence_identity import evidence_id; from app.report_v22.evidence_models import EvidenceObservation; import sys; print(evidence_id(EvidenceObservation.model_validate_json(sys.stdin.read())))"
    actual=subprocess.check_output([sys.executable,"-c",code],input=original.model_dump_json().encode()).decode().strip()
    assert actual==evidence_id(original)


@pytest.mark.parametrize("update",[
    {"source_type":"competitor"},
    {"source_locator":SourceLocator(query="different")},
    {"source_locator":SourceLocator(latitude=1.0,longitude=2.0)},
    {"source_locator":SourceLocator(device="desktop")},
    {"selector":EvidenceSelector(category="metric",record_key="row",field="clicks",unit="percent")},
    {"selector":EvidenceSelector(category="site_field",record_key="page",field="status_code",competitor_id="cp_other")},
])
def test_identity_covers_logical_context(update):
    assert evidence_id(observation())!=evidence_id(observation(**update))
