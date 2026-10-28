"""Synthetic exact GA4 request/response material; never live customer data."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from uuid import UUID
import json

import httpx

from app.google_connections_v22.ga4 import normalize_report
from app.google_connections_v22.ga4_comparison import query_body
from app.jobs_v22.digest import verified_request_digest, verified_canonical_json_bytes
from app.report_v22.ga4_comparison_collection import prepare_ga4_comparison
from app.report_v22.ga4_comparison_models import (
    Ga4ComparisonScope, Ga4ComparisonCollection, Ga4ExactSnapshot, qualify_ga4,
)
from app.report_v22.implementation_records import ImplementationRecord
from tests.support.comparison_fixtures import request_raw

AT = "2026-12-15T08:00:00Z"
RESOURCE = "properties/12345"
BINDING = UUID(int=20003)


def record(**changes):
    raw = deepcopy(request_raw()["implementation"])
    raw["entries"][0].update(changes)
    raw["checksum"] = verified_request_digest({k: v for k, v in raw.items() if k != "checksum"})
    return ImplementationRecord.model_validate_json(verified_canonical_json_bytes(raw))


def scope(**changes):
    return Ga4ComparisonScope(resource_id=RESOURCE, hosts=["example.test"], timezone="America/Los_Angeles",
        channel="organic_search", landing_path=None).model_copy(update=changes)


def options(**changes):
    return dict(action_id="ac-1", days=28, binding_id=BINDING, scope=scope(), at=AT) | changes


def report_payload(body):
    names = [d["name"] for d in body["dimensions"]]
    metrics = [m["name"] for m in body["metrics"]]
    window = body["dateRanges"][0]
    first, last = date.fromisoformat(window["startDate"]), date.fromisoformat(window["endDate"])
    keys = [[(first+timedelta(days=i)).strftime("%Y%m%d")] for i in range((last-first).days+1)] if names == ["date"] else [["contact"]] if names else [[]]
    values = dict(sessions="10", activeUsers="8", engagedSessions="7", engagementRate="0.7", averageSessionDuration="42.5",
        screenPageViews="15", keyEvents="2", sessionKeyEventRate="0.2")
    return dict(dimensionHeaders=[dict(name=n) for n in names], metricHeaders=[dict(name=n) for n in metrics],
        rows=[dict(dimensionValues=[dict(value=k) for k in row], metricValues=[dict(value=values[m]) for m in metrics]) for row in keys],
        rowCount=len(keys), metadata=dict(timeZone="America/Los_Angeles"))


def google(request):
    if request.method == "GET":
        return httpx.Response(200, json={"keyEvents": [dict(name=f"{RESOURCE}/keyEvents/1", eventName="contact",
            countingMethod="ONCE_PER_EVENT", custom=True, deletable=True, createTime="2026-01-01T00:00:00Z")]})
    return httpx.Response(200, json=report_payload(json.loads(request.content)))


def fixture():
    plan = prepare_ga4_comparison(record(), **options())
    periods = []
    for window in (plan.windows.before, plan.windows.after):
        views = {v: normalize_report(report_payload(query_body(plan, window, v)), view=v, start=window.start, end=window.end).model_dump(mode="json")
            for v in ("totals", "dates", "key_events")}
        periods.append(dict(start_date=window.start.isoformat(), end_date=window.end.isoformat(), **views))
    observation = dict(observed_at=AT, events=[dict(event_name="contact", counting_method="ONCE_PER_EVENT",
        custom=True, deletable=True, create_time="2026-01-01T00:00:00Z")])
    snapshot = Ga4ExactSnapshot.model_validate_json(json.dumps(dict(schema_version="ga4_comparison_observation_v1",
        scope=plan.scope.model_dump(mode="json"), windows=plan.windows.model_dump(mode="json"), started_at=AT, completed_at=AT,
        previous=periods[0], current=periods[1], configuration_before=observation, configuration_after=observation)))
    raw = dict(schema_version="v22_ga4_comparison_collection_v1", plan=plan.model_dump(mode="json"), state="collected",
        snapshot=snapshot.model_dump(mode="json"), qualification=qualify_ga4(snapshot).model_dump(mode="json"))
    raw["checksum"] = verified_request_digest(raw)
    return Ga4ComparisonCollection.model_validate_json(verified_canonical_json_bytes(raw))
