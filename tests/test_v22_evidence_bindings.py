from datetime import timedelta
from uuid import UUID
import pytest

from app.jobs_v22.digest import request_digest
from app.report_v22.evidence_bindings import validate_sources, eligibility
from app.report_v22.evidence_errors import EvidenceError
from evidence_helpers import build_input, site_source, serp_source, first_party_source, NOW


def test_valid_public_bindings_and_stable_registry():
    site, serp = site_source(), serp_source()
    assert len(validate_sources(build_input(site, serp, site))) == 2


@pytest.mark.parametrize("field,value,code", [
    ("case_id", UUID(int=99), "SNAPSHOT_BINDING_INVALID"),
    ("source_type", "serp", "SNAPSHOT_BINDING_INVALID"),
    ("schema_version", "wrong", "SNAPSHOT_BINDING_INVALID"),
    ("payload_checksum", "sha256:"+"0"*64, "CHECKSUM_MISMATCH"),
    ("fetched_at", NOW+timedelta(days=1), "SNAPSHOT_BINDING_INVALID"),
])
def test_rejects_bad_binding(field, value, code):
    source = site_source()
    source.binding = source.binding.model_copy(update={field:value})
    with pytest.raises(EvidenceError, match=code):
        validate_sources(build_input(source))


def test_rejects_tampering_and_wrong_context():
    site = site_source()
    site.payload.pages[0].title = "changed"
    with pytest.raises(EvidenceError, match="CHECKSUM_MISMATCH"):
        validate_sources(build_input(site))
    with pytest.raises(EvidenceError, match="SNAPSHOT_BINDING_INVALID"):
        validate_sources(build_input(serp_source(), search_device="desktop"))


def test_same_uuid_different_envelope_metadata_is_rejected():
    a = first_party_source()
    b = a.model_copy(deep=True)
    b.payload.provider_request_context.external_resource_id = "another-resource"
    assert request_digest(a.payload.normalized_payload) == request_digest(b.payload.normalized_payload)
    with pytest.raises(EvidenceError, match="SNAPSHOT_BINDING_INVALID"):
        validate_sources(build_input(a,b,report_type="verified_execution"))


def test_prospect_rejects_first_party_and_expiry_boundary_is_fixed():
    fp = first_party_source()
    with pytest.raises(EvidenceError, match="SNAPSHOT_BINDING_INVALID"):
        validate_sources(build_input(fp))
    assert eligibility(fp, NOW) is None
    assert eligibility(fp, fp.binding.expires_at) == "expired"
    site = site_source()
    assert eligibility(site, NOW+timedelta(days=100)) is None


@pytest.mark.parametrize("changes,reason", [({"health_status":"unhealthy"},"unhealthy"), ({"identity_match_status":"mismatch"},"identity_mismatch"), ({"identity_match_status":"not_checked"},"identity_unconfirmed")])
def test_first_party_eligibility(changes,reason):
    assert eligibility(first_party_source(**changes),NOW) == reason


def test_shared_market_retains_original_uuid_and_source_job():
    from app.competitors_v22.models import SharedMarketSnapshot
    source=serp_source()
    source.binding.expires_at=NOW+timedelta(days=1)
    source.shared_snapshot=SharedMarketSnapshot(schema_version="competitor_shared_market_v1",snapshot_id=source.binding.snapshot_id,
        source_job_id=source.payload.job_id,input_digest=request_digest({"synthetic":"authorized context"}),
        snapshot_checksum=source.binding.payload_checksum,created_at=source.binding.fetched_at,expires_at=source.binding.expires_at,snapshot=source.payload)
    assert validate_sources(build_input(source))[0].binding.snapshot_id==UUID(int=12)
    source.binding.snapshot_id=UUID(int=999)
    with pytest.raises(EvidenceError,match="SNAPSHOT_BINDING_INVALID"):
        validate_sources(build_input(source))


def test_collection_requires_matching_market_and_confirmed_competitors():
    from evidence_helpers import competitor_source
    market=serp_source()
    collection=competitor_source(market)
    assert len(validate_sources(build_input(market,collection)))==2
    with pytest.raises(EvidenceError,match="SNAPSHOT_BINDING_INVALID"):
        validate_sources(build_input(collection))
    wrong=build_input(market,collection)
    wrong.context.competitors[0].business_name="Unconfirmed identity"
    with pytest.raises(EvidenceError,match="SNAPSHOT_BINDING_INVALID"):
        validate_sources(wrong)


def test_first_party_impossible_dates_duplicate_dimensions_and_nan_rejected():
    from app.api.v2.models import DimensionValue
    from app.report_v22.evidence import build_evidence_index
    for mutate in (
        lambda s:setattr(s.payload.provider_request_context,"start_date","2026-02-30"),
        lambda s:s.payload.normalized_payload.rows[0].dimensions.append(DimensionValue(key="query",value="duplicate")),
        lambda s:setattr(s.payload.normalized_payload.aggregates[0],"value",float("nan")),
    ):
        value=build_input(first_party_source(),report_type="verified_execution")
        mutate(value.sources[0])
        with pytest.raises(EvidenceError):
            build_evidence_index(value)


def test_snapshot_internal_reorder_without_resealing_is_tampering():
    source=serp_source()
    source.payload.result_counts.reverse()
    with pytest.raises(EvidenceError,match="CHECKSUM_MISMATCH"):
        validate_sources(build_input(source))
