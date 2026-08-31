"""Synthetic inputs only: UUIDs are fixture identities, not stored snapshots."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.api.v2.models import FirstPartySnapshotEnvelope, NormalizedSnapshotPayload, ProviderRequestContext, SnapshotMetric, SnapshotRow, DimensionValue
from app.jobs_v22.digest import request_digest
from app.report_v22.evidence_models import EvidenceBuildContext, EvidenceBuildInput, SnapshotBinding, SiteEvidenceSource, SerpEvidenceSource, FirstPartyEvidenceSource
from test_v22_site_inventory_models import snapshot as site_snapshot, page, selected
from test_v22_serp_market_models import snapshot as serp_snapshot
from test_v22_competitor_models import confirmed

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)
CASE = UUID("11111111-1111-4111-8111-111111111111")


def context(**updates):
    values = dict(case_id=CASE, report_type="prospect", site_url="https://example.test/", primary_service="Plumber",
        target_market=dict(display_name="Austin, TX", country_code="US", latitude=30.2672, longitude=-97.7431),
        queries=["plumber", "emergency plumber", "water heater repair"], competitors=[confirmed(i) for i in range(1, 4)], evaluated_at=NOW)
    values.update(updates)
    return EvidenceBuildContext.model_validate(values)


def bind(payload, kind, number, **updates):
    first = kind in {"gsc", "gbp", "ga4"}
    values = dict(snapshot_id=payload.snapshot_id if first else UUID(int=number), case_id=CASE, source_type=kind,
        schema_version=payload.schema_version, payload_checksum=request_digest(payload.normalized_payload if first else payload),
        fetched_at=payload.fetched_at if first else payload.completed_at)
    if first:
        values.update(expires_at=payload.expires_at, health_status=payload.health_status, identity_match_status=payload.identity_match_status)
    values.update(updates)
    return SnapshotBinding(**values)


def site_source():
    url = "https://example.test/"
    payload = site_snapshot(root_url=url, canonical_host="example.test", pages=[page(url)], selected_pages=[selected(url)])
    return SiteEvidenceSource(binding=bind(payload, "site", 11), payload=payload)


def serp_source():
    raw = serp_snapshot().model_dump(mode="json")
    for run in raw["query_runs"]:
        for result in run["results"]:
            result.update(url="https://competitor-1.test/", normalized_domain="competitor-1.test")
    payload = type(serp_snapshot()).model_validate_json(__import__("json").dumps(raw))
    return SerpEvidenceSource(binding=bind(payload, "serp", 12), payload=payload)


def first_party_source(kind="gsc", **updates):
    normalized = NormalizedSnapshotPayload(aggregates=[SnapshotMetric(key="clicks", value=0.0, unit="count")],
        rows=[SnapshotRow(dimensions=[DimensionValue(key="query", value="emergency plumber"), DimensionValue(key="device", value="tablet")], metrics=[SnapshotMetric(key="clicks", value=5.0, unit="count")])])
    values = dict(snapshot_id=UUID(int={"gsc":21,"gbp":22,"ga4":23}[kind]), source_type=kind, schema_version="first_party_snapshot_v1",
        fetched_at=NOW-timedelta(hours=1), expires_at=NOW+timedelta(hours=1), identity_match_status="matched", health_status="healthy",
        normalized_payload=normalized, provider_request_context=ProviderRequestContext(external_resource_id="fixture-resource",start_date="2026-08-01",end_date="2026-08-29"), payload_checksum=request_digest(normalized))
    values.update(updates)
    payload = FirstPartySnapshotEnvelope(**values)
    return FirstPartyEvidenceSource(binding=bind(payload, kind, 21), payload=payload)


def build_input(*sources, **context_updates):
    return EvidenceBuildInput(context=context(**context_updates), sources=list(sources))


def deep_site_source(text="We repair plumbing systems for homes in Austin."):
    from app.collectors.site_inventory_models import DeepPageSnapshot
    source = site_source()
    p = source.payload
    p.selected_pages[0].deep_analyzed = True
    p.selected_pages[0].deep_snapshot = DeepPageSnapshot(url=p.root_url, final_url=p.root_url,
        page_type="home",crawl_depth=0,collected_at=p.completed_at,status_code=200,content_type="text/html",
        response_bytes=len(text.encode()),content_checksum=request_digest({"fixture_raw_response":text}),html="<p>Synthetic page</p>",text=text)
    p.deep_analyzed_count = 1
    source.binding = bind(p,"site",11)
    return source


def competitor_source(serp=None):
    from app.competitors_v22.models import CompetitorCollectionSnapshot, CompetitorCollectionBudget, CompetitorSnapshot, PublicGbpProfile, PublicReviewRecord
    from app.report_v22.evidence_models import CompetitorEvidenceSource
    serp = serp or serp_source()
    competitors=[]
    for i in range(1,4):
        item = confirmed(i)
        competitors.append(CompetitorSnapshot(competitor=item,query_appearance_count=3,best_position=i,analyzed_page_count=0,
            site_status="unavailable",public_gbp_status="available",reviews_status="partial",
            public_gbp=PublicGbpProfile(business_name=item.business_name,website_url=item.website_url,rating=4.5,review_count=30,collected_at=NOW),
            reviews=[PublicReviewRecord(review_record_id="rv_"+str(i)*16,provider_review_id=f"fixture-review-{i}",rating=5.0,
                text="The plumber arrived as scheduled.",owner_response_text="Thank you for your feedback.",source="google_maps_reviews",collected_at=NOW,request_record_id="req_"+str(i)*16)],
            limitations=["Synthetic partial review sample."]))
    p = CompetitorCollectionSnapshot(schema_version="competitor_collection_snapshot_v1",job_id=UUID(int=31),discovery_id=UUID(int=32),
        candidate_digest=request_digest([c.model_dump(mode="json") for c in competitors]),market_snapshot_id=serp.binding.snapshot_id,
        market_snapshot_checksum=serp.binding.payload_checksum,started_at=NOW,completed_at=NOW,competitors=competitors,
        budget=CompetitorCollectionBudget(site_discovery_limit_each=50,site_deep_limit_each=10,review_sample_limit_each=30,
            provider_attempt_limit=15,provider_attempts_used=6,place_detail_calls=3,review_page_calls=3,checkpoint_hits=0,truncated=False))
    return CompetitorEvidenceSource(binding=bind(p,"competitor",13),payload=p)
