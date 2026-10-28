"""Deterministic synthetic source snapshots; never a provider or product entry."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID

from app.google_connections_v22.gsc import GscSnapshot, evaluate_health
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.report_v22.comparison_models import ComparisonRequest
from app.report_v22.first_party_findings_models import TrustedFirstPartySnapshot

FIXTURE = Path(__file__).resolve().parents[2]/"contracts/v2.2-implementation/fixture.json"


def saved_gsc(case_id, *, after=False, daily_clicks=12, daily_impressions=240, end=None):
    end = end or (date(2026, 10, 19) if after else date(2026, 8, 31))
    def view(rows, aggregation="byProperty"):
        return dict(rows=rows, truncated=False, aggregation_type=aggregation)
    def period(last):
        first = last-timedelta(days=89)
        rows = []
        for i in range(90):
            day = first+timedelta(days=i)
            click, imp = (daily_clicks, daily_impressions) if day >= date(2026, 9, 2) else (10, 200)
            rows.append(dict(key=day.isoformat(), clicks=click, impressions=imp, ctr=click/imp if imp else 0, position=3))
        clicks, impressions = sum(r["clicks"] for r in rows), sum(r["impressions"] for r in rows)
        total = dict(clicks=clicks, impressions=impressions, ctr=clicks/impressions if impressions else 0, position=3)
        return dict(start_date=first.isoformat(), end_date=last.isoformat(), totals=view([dict(key=None, **total)]),
            pages=view([dict(key="https://example.test/service", **total)], "byPage"), queries=view([dict(key="repair service", **total)]),
            devices=view([dict(key="MOBILE", **total)]), countries=view([dict(key="usa", **total)]), dates=view(rows))
    value = GscSnapshot.model_validate_json(json.dumps(dict(schema_version="gsc_sync_v1", resource_id="sc-domain:example.test",
        current=period(end), previous=period(end-timedelta(days=90)), limitations=["GSC_QUERY_PRIVACY_FILTERING", "GSC_WEB_ONLY"])))
    fetched = datetime.combine(end+timedelta(days=2), datetime.min.time(), timezone.utc)+timedelta(hours=8)
    payload = value.model_dump(mode="json")
    health, reasons = evaluate_health(value)
    return TrustedFirstPartySnapshot(snapshot_id=UUID(int=20002 if after else 20001), case_id=case_id, binding_id=UUID(int=20003),
        source_type="gsc", schema_version="gsc_sync_v1", fetched_at=fetched, expires_at=fetched+timedelta(days=7),
        identity_match_status="matched", health_status=health, health_reasons=reasons, normalized_payload=payload,
        payload_checksum=request_digest(payload), external_resource_id=value.resource_id,
        coverage_start=value.previous.start_date, coverage_end=value.current.end_date)


def request_raw():
    record = deepcopy(json.loads(FIXTURE.read_text())["history"][0])
    record["entries"][0].update(implemented_on="2026-09-01", timezone="America/Los_Angeles")
    record["checksum"] = verified_request_digest({k:v for k,v in record.items() if k != "checksum"})
    case_id = UUID(record["case_id"])
    return dict(schema_version="v22_comparison_request_v1", implementation=record, action_id=record["entries"][0]["action_id"], days=28,
        evaluated_at="2026-10-22T08:00:00Z", scope=dict(source="gsc", dimension="property", key=None, channel="organic_search", filters=[]),
        before=saved_gsc(case_id).model_dump(mode="json"), after=saved_gsc(case_id, after=True).model_dump(mode="json"), concurrent_changes="unknown")


def resign_source(source):
    from app.google_connections_v22.ga4 import Ga4Snapshot, evaluate_health as ga4_health
    model, health = (GscSnapshot, evaluate_health) if source["source_type"] == "gsc" else (Ga4Snapshot, ga4_health)
    value = model.model_validate_json(json.dumps(source["normalized_payload"]))
    source["health_status"], source["health_reasons"] = health(value)
    source["payload_checksum"] = request_digest(source["normalized_payload"])
    return source


def ga4_request_raw():
    from app.google_connections_v22.ga4 import Ga4Snapshot
    raw = request_raw()
    raw["scope"].update(source="ga4", channel="all")
    for name in ("before", "after"):
        source = raw[name]
        previous = source["normalized_payload"]
        def period(p):
            metadata = dict(time_zone="America/Los_Angeles")
            metrics = dict(sessions=1800, active_users=1500, engaged_sessions=900, engagement_rate=.5,
                average_session_duration=45, screen_page_views=2000, key_events=90., session_key_event_rate=.05)
            landing = {k:v for k,v in metrics.items() if k != "active_users"}
            return dict(start_date=p["start_date"], end_date=p["end_date"], totals=dict(rows=[metrics], metadata=metadata),
                dates=dict(rows=[dict(date=row["key"], sessions=20, engaged_sessions=10, key_events=1.) for row in p["dates"]["rows"]], metadata=metadata),
                landing_pages=dict(rows=[dict(landing_page="/service", **landing)], truncated=False, metadata=metadata),
                key_events=dict(rows=[dict(event_name="contact", key_events=90.)], truncated=False, metadata=metadata))
        value = Ga4Snapshot.model_validate_json(json.dumps(dict(schema_version="ga4_sync_v1", resource_id="properties/12345",
            host_filter=["example.test"], configured_key_events=[dict(event_name="contact", counting_method="ONCE_PER_EVENT", custom=True, deletable=True)],
            current=period(previous["current"]), previous=period(previous["previous"]), limitations=["GA4_HOST_FILTERED", "GA4_ESTIMATED_COUNTS"])))
        source.update(source_type="ga4", schema_version="ga4_sync_v1", external_resource_id=value.resource_id,
            normalized_payload=value.model_dump(mode="json"))
        resign_source(source)
    return raw


def parsed(raw):
    return ComparisonRequest.model_validate_json(canonical_json_bytes(raw))
