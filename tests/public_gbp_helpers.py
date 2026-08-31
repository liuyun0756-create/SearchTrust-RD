"""Synthetic normalized observations, not a provider decoder or stored records."""
from datetime import timedelta
from uuid import UUID

from evidence_helpers import CASE, NOW, context


def sample_input():
    target = {"public_gbp_url": "https://maps.google.com/?cid=12345",
              "entity_keys": [{"kind": "place_id", "value": "fixture-customer-place"}, {"kind": "cid", "value": "12345"}]}
    values = {"business_name": "Example Plumbing", "website_url": "https://example.test/",
              "address": "123 Fixture Street, Austin, TX", "phone": "+1 512 555 0100",
              "service_areas": ["Austin", "Round Rock"], "service_area_business": False}
    return dict(reference={**target, "case_id": CASE, "site_url": "https://example.test/",
                           "confirmation_source": "user", "confirmed_at": NOW - timedelta(hours=2)},
                request_target=target, started_at=NOW - timedelta(hours=1), completed_at=NOW - timedelta(minutes=30),
                expires_at=NOW + timedelta(days=1), provider="serpapi_public", request_record_id="req_" + "a" * 24,
                response_checksum="sha256:" + "b" * 64, collection_status="succeeded", failure_code=None,
                record={"observed_entity_keys": [dict(key) for key in target["entity_keys"]],
                        "observed_public_gbp_url": target["public_gbp_url"],
                        "fields": {key: {"state": "observed", "value": value} for key, value in values.items()}})


def public_source(value=None, number=71):
    from app.jobs_v22.digest import request_digest
    from app.report_v22.evidence_models import PublicGbpEvidenceSource, SnapshotBinding
    from app.report_v22.public_gbp_snapshot import build_customer_public_gbp_snapshot
    value = value or sample_input()
    payload = build_customer_public_gbp_snapshot(value)
    return PublicGbpEvidenceSource(payload=payload, binding=SnapshotBinding(
        snapshot_id=UUID(int=number), case_id=CASE, source_type="gbp", schema_version=payload.schema_version,
        payload_checksum=request_digest(payload), fetched_at=payload.completed_at, expires_at=payload.expires_at,
        health_status=payload.health_status, identity_match_status=payload.identity_match_status))


def evidence_input(value=None, *other_sources):
    from app.report_v22.evidence_models import EvidenceBuildInput
    value = value or sample_input()
    return EvidenceBuildInput(context=context(customer_public_gbp=value["reference"]),
                              sources=[public_source(value), *other_sources])
