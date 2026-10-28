"""Bounded exact-window organic GA4 collection; ordinary sync stays unchanged."""
import asyncio
from datetime import datetime, timezone

from app.google_connections_v22.ga4 import (
    DATA_ORIGIN, REPORTS, Ga4Provider, SyncError, _classify, normalize_report,
)
from app.google_connections_v22.gsc import costed_google_json
from app.jobs_v22.cost_ledger import JobCostLedger
from app.report_v22.capability import instant
from app.report_v22.ga4_comparison_models import (
    Ga4ComparisonPlan, Ga4ConfigurationObservation, Ga4ExactPeriod, Ga4ExactSnapshotV2,
)
from app.report_v22.ga4_truncation import truncation_check


def query_body(plan: Ga4ComparisonPlan, period, view: str) -> dict:
    dimensions, metrics, limit = REPORTS[view]
    expressions = Ga4Provider._filter(plan.scope.hosts)["andGroup"]["expressions"]
    expressions.append({"filter": {"fieldName": "sessionDefaultChannelGroup", "stringFilter": {
        "matchType": "EXACT", "value": "Organic Search", "caseSensitive": True}}})
    if plan.scope.landing_path is not None:
        expressions.append({"filter": {"fieldName": "landingPage", "stringFilter": {
            "matchType": "EXACT", "value": plan.scope.landing_path, "caseSensitive": True}}})
    body = {"dateRanges": [{"startDate": period.start.isoformat(), "endDate": period.end.isoformat()}],
        "dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics],
        "dimensionFilter": {"andGroup": {"expressions": expressions}}, "offset": "0",
        "limit": str(limit+1 if view == "key_events" else limit), "keepEmptyRows": False}
    if view == "dates": body["orderBys"] = [{"dimension": {"dimensionName": "date"}, "desc": False}]
    if view == "key_events":
        body["orderBys"] = [{"metric": {"metricName": "keyEvents"}, "desc": True}]
        body["metricFilter"] = {"filter": {"fieldName": "keyEvents", "numericFilter": {
            "operation": "GREATER_THAN", "value": {"int64Value": "0"}}}}
    return body


class Ga4ComparisonProvider(Ga4Provider):
    def __init__(self, client, *, clock=None):
        super().__init__(client)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise SyncError("SYNC_INVALID_CLOCK")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    async def collect_exact(self, plan: Ga4ComparisonPlan, token: str, *, cost_ledger: JobCostLedger) -> Ga4ExactSnapshotV2:
        plan = Ga4ComparisonPlan.model_validate_json(plan.model_dump_json())
        if plan.state != "collectable": raise SyncError("SYNC_WINDOW_NOT_ELAPSED")
        if cost_ledger is None: raise SyncError("SYNC_COST_LEDGER_REQUIRED")
        if not isinstance(token, str) or not token or any(ord(c) < 33 for c in token):
            raise SyncError("SYNC_INVALID_TOKEN")
        started = self._now()
        if instant(started) < instant(plan.prepared_at): raise SyncError("SYNC_INVALID_CLOCK")
        try:
            async with asyncio.timeout(240):
                before_events = await self._configured_events(plan.scope.resource_id, token, cost_ledger=cost_ledger)
                before = Ga4ConfigurationObservation(observed_at=self._now(), events=before_events)
                periods, truncation_checks = [], []
                for period_name, period in (("previous", plan.windows.before), ("current", plan.windows.after)):
                    views = {}
                    for view in ("totals", "dates", "key_events"):
                        status, payload = await costed_google_json(self.client, "POST",
                            f"{DATA_ORIGIN}/{plan.scope.resource_id}:runReport",
                            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
                            body=query_body(plan, period, view), operation="ga4_run_report", cost_ledger=cost_ledger)
                        _classify(status, payload)
                        parsed = normalize_report(payload, view=view, start=period.start, end=period.end)
                        truncation_checks.append(truncation_check(payload["metadata"], period=period_name, view=view))
                        # Do not accept coerced metadata flags or silently partial aggregate/date reports.
                        if any(k in payload["metadata"] and type(payload["metadata"][k]) is not bool
                            for k in ("subjectToThresholding", "dataLossFromOtherRow")):
                            raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE")
                        row_count, row_length = payload.get("rowCount", 0), len(payload.get("rows", []))
                        if view != "key_events" and row_count != row_length:
                            raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE")
                        if view == "key_events" and row_count > row_length:
                            parsed.truncated = True
                        if parsed.metadata.time_zone != plan.scope.timezone:
                            raise SyncError("SYNC_GA4_TIMEZONE_CHANGED")
                        views[view] = parsed
                    periods.append(Ga4ExactPeriod(start_date=period.start, end_date=period.end, **views))
                after_events = await self._configured_events(plan.scope.resource_id, token, cost_ledger=cost_ledger)
                after = Ga4ConfigurationObservation(observed_at=self._now(), events=after_events)
                return Ga4ExactSnapshotV2(schema_version="ga4_comparison_observation_v2", scope=plan.scope, windows=plan.windows,
                    started_at=started, completed_at=self._now(), previous=periods[0], current=periods[1],
                    configuration_before=before, configuration_after=after, truncation_checks=truncation_checks)
        except TimeoutError:
            raise SyncError("SYNC_TIMEOUT", True) from None
        except (ValueError, TypeError, OverflowError):
            raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE") from None
