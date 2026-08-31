import pytest

from app.report_v22.evidence import build_evidence_index
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.evidence_models import EvidenceBuildLimits, MissingEvidenceSource
from evidence_helpers import build_input, deep_site_source, site_source, serp_source, competitor_source, first_party_source


def test_public_build_is_sorted_repeatable_nonmutating_and_trace_complete():
    market=serp_source()
    value=build_input(deep_site_source(),market,competitor_source(market))
    before=value.model_dump_json()
    result=build_evidence_index(value)
    assert value.model_dump_json()==before
    assert result==build_evidence_index(value)
    value.sources.reverse()
    assert result==build_evidence_index(value)
    ids=[e.evidence_id for e in result.evidence_index]
    assert ids==sorted(set(ids))
    assert ids==[t.evidence_id for t in result.source_traces]
    assert all(e.source_locator.field_path.startswith("selector:") for e in result.evidence_index)


def test_missing_snapshot_only_makes_gap_not_evidence():
    value=build_input()
    value.missing_sources=[MissingEvidenceSource(source_type="gsc",reason="not_connected")]
    result=build_evidence_index(value)
    assert not result.evidence_index
    assert result.coverage_gaps[0].snapshot_id is None


@pytest.mark.parametrize("changes,reason",[({"health_status":"unhealthy"},"unhealthy"),({"identity_match_status":"mismatch"},"identity_mismatch"),({"identity_match_status":"needs_confirmation"},"identity_unconfirmed")])
def test_ineligible_first_party_only_produces_real_snapshot_coverage(changes,reason):
    result=build_evidence_index(build_input(first_party_source(**changes),report_type="verified_execution"))
    assert {e.source_type for e in result.evidence_index}=={"coverage"}
    assert result.coverage_gaps[0].reason==reason
    assert not result.source_summaries[0].business_eligible


@pytest.mark.parametrize("limits",[EvidenceBuildLimits(max_items=1),EvidenceBuildLimits(max_bytes=10)])
def test_resource_limits_are_fail_closed(limits):
    value=build_input(site_source())
    value.limits=limits
    with pytest.raises(EvidenceError,match="LIMIT_EXCEEDED"):
        build_evidence_index(value)


def test_dedup_merges_paths_and_collision_rejects_conflicting_fields(monkeypatch):
    from app.report_v22.evidence_adapters import site
    source=deep_site_source("Duplicate plumbing service description.\nDuplicate plumbing service description.")
    result=build_evidence_index(build_input(source))
    assert sum(t.selector.category=="page_fragment" for t in result.source_traces)==1
    real=list(site.observations(source))[0]
    duplicate=real.model_copy(deep=True)
    duplicate.origin_paths=["/payload/pages/0/status_code"]
    monkeypatch.setattr(site,"observations",lambda _:iter([real,duplicate]))
    merged=build_evidence_index(build_input(source))
    assert len(merged.source_traces[0].origin_paths)==2
    duplicate.original_value="different raw value"
    with pytest.raises(EvidenceError,match="ID_CONFLICT"):
        build_evidence_index(build_input(source))


def test_safe_errors_never_echo_source_and_validation_precedes_adapters(monkeypatch):
    from app.report_v22.evidence_adapters import site
    secret="PRIVATE_SOURCE_SENTINEL"
    source=site_source()
    source.payload.pages[0].title=secret
    monkeypatch.setattr(site,"observations",lambda _:pytest.fail("adapter ran before validation"))
    with pytest.raises(EvidenceError) as exc:
        build_evidence_index(build_input(source))
    assert secret not in str(exc.value) and secret not in exc.value.user_message
    malformed=build_input().model_dump(mode="python")
    malformed["sources"]=[{"kind":"site","payload":{"private":secret}}]
    with pytest.raises(EvidenceError,match="SOURCE_INVALID") as exc:
        build_evidence_index(malformed)
    assert secret not in str(exc.value)


def test_all_limitations_retained_in_summary_with_bounded_evidence():
    source=site_source()
    source.payload.limitations=[f"Limitation {i:02}" for i in range(30)]
    from evidence_helpers import bind
    source.binding=bind(source.payload,"site",11)
    result=build_evidence_index(build_input(source))
    assert len(result.source_summaries[0].limitations)==30
    assert all(len(e.limitations)==20 for e in result.evidence_index)


@pytest.mark.parametrize("kind",["site","serp","competitor","gsc","gbp","ga4"])
def test_expiry_gates_every_source_type(kind):
    from datetime import timedelta
    from evidence_helpers import NOW
    market=serp_source()
    source={"site":site_source,"serp":serp_source,"competitor":competitor_source}.get(kind,lambda:first_party_source(kind))()
    expiry=NOW+timedelta(minutes=1)
    source.binding.expires_at=expiry
    if source.kind=="first_party":
        source.payload.expires_at=expiry
    inputs=[source] if kind!="competitor" else [market,source]
    result=build_evidence_index(build_input(*inputs,report_type="verified_execution",evaluated_at=expiry))
    owned=[e for e in result.evidence_index if e.snapshot_id==source.binding.snapshot_id]
    assert len(owned)==(3 if kind=="competitor" else 1)
    assert all(e.source_type=="coverage" for e in owned)
    assert owned[0].normalized_value=="expired"


def test_invalid_origin_is_not_returned(monkeypatch):
    from app.report_v22.evidence_adapters import site
    source=site_source()
    item=next(site.observations(source))
    item.origin_paths=["/payload/missing"]
    monkeypatch.setattr(site,"observations",lambda _:iter([item]))
    with pytest.raises(EvidenceError,match="SOURCE_INVALID"):
        build_evidence_index(build_input(source))


def test_no_bound_source_for_missing_gap_and_no_random_uuid_allocation():
    value=build_input(site_source())
    value.missing_sources=[MissingEvidenceSource(source_type="site")]
    with pytest.raises(EvidenceError,match="SNAPSHOT_BINDING_INVALID"):
        build_evidence_index(value)


def test_different_dimensions_units_and_aggregate_row_do_not_merge():
    from app.api.v2.models import SnapshotMetric, SnapshotRow
    from evidence_helpers import bind
    source=first_party_source()
    p=source.payload.normalized_payload
    p.rows.append(SnapshotRow(dimensions=[],metrics=[SnapshotMetric(key="clicks",value=0.0,unit="count"),SnapshotMetric(key="clicks",value=0.0,unit="percent")]))
    from app.jobs_v22.digest import request_digest
    source.payload.payload_checksum=request_digest(p)
    source.binding=bind(source.payload,"gsc",21)
    result=build_evidence_index(build_input(source,report_type="verified_execution"))
    assert len(result.evidence_index)==4


def test_strict_scalar_conflict_uses_type_not_python_equality(monkeypatch):
    from app.report_v22.evidence_adapters import site
    source=site_source()
    a=next(site.observations(source))
    a.original_value=True
    b=a.model_copy(update={"original_value":1})
    monkeypatch.setattr(site,"observations",lambda _:iter([a,b]))
    with pytest.raises(EvidenceError,match="ID_CONFLICT"):
        build_evidence_index(build_input(source))


def test_byte_limit_stops_extraction_before_materializing_all_observations(monkeypatch):
    from app.report_v22.evidence_adapters import site
    source=site_source()
    item=next(site.observations(source))
    def oversized(_):
        yield item
        pytest.fail("continued extracting after the retained output exceeded its limit")
    monkeypatch.setattr(site,"observations",oversized)
    value=build_input(source)
    value.limits=EvidenceBuildLimits(max_bytes=100)
    with pytest.raises(EvidenceError,match="LIMIT_EXCEEDED"):
        build_evidence_index(value)
