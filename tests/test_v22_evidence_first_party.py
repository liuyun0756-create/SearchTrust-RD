import pytest
from app.report_v22.evidence_adapters.first_party import observations
from evidence_helpers import first_party_source


@pytest.mark.parametrize("kind",["gsc","gbp","ga4"])
def test_first_party_rows_aggregates_and_real_zero(kind):
    items=list(observations(first_party_source(kind)))
    assert len(items)==2
    assert {i.normalized_value for i in items} == {0.0,5.0}
    row=next(i for i in items if i.normalized_value==5.0)
    assert row.source_locator.device is None
    assert row.source_locator.query == "emergency plumber"
    assert {d.key:d.value for d in row.selector.dimensions}["device"] == "tablet"
    assert any("tablet" in note for note in row.limitations)
    assert all(i.confidence == "high" for i in items)


@pytest.mark.parametrize("kind",["gsc","gbp","ga4"])
def test_empty_payload_has_coverage_without_fabricated_metrics(kind):
    from app.api.v2.models import NormalizedSnapshotPayload
    from app.jobs_v22.digest import request_digest
    from app.report_v22.evidence import build_evidence_index
    from evidence_helpers import bind, build_input
    source=first_party_source(kind)
    source.payload.normalized_payload=NormalizedSnapshotPayload()
    source.payload.payload_checksum=request_digest(source.payload.normalized_payload)
    source.binding=bind(source.payload,kind,21)
    result=build_evidence_index(build_input(source,report_type="verified_execution"))
    assert len(result.evidence_index)==1
    assert result.evidence_index[0].normalized_value=="empty"
    assert result.evidence_index[0].original_value is None


def test_long_query_and_provider_tablet_context_are_retained_without_truncation():
    from app.api.v2.models import DimensionValue
    from app.jobs_v22.digest import request_digest
    from app.report_v22.evidence import build_evidence_index
    from evidence_helpers import bind, build_input
    source=first_party_source()
    source.payload.provider_request_context.device="tablet"
    source.payload.normalized_payload.rows[0].dimensions=[DimensionValue(key="query",value="x"*500)]
    source.payload.payload_checksum=request_digest(source.payload.normalized_payload)
    source.binding=bind(source.payload,"gsc",21)
    result=build_evidence_index(build_input(source,report_type="verified_execution"))
    assert all(e.source_locator.device is None for e in result.evidence_index)
    row=next(t for t in result.source_traces if t.selector.dimensions)
    assert row.selector.dimensions[0].value=="x"*500
    assert row.selector.request_context.device=="tablet"
    assert all(len(e.source_locator.field_path)<=500 for e in result.evidence_index)
