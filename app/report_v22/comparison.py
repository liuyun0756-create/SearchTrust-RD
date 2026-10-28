"""Pure descriptive comparisons over existing, validated immutable snapshots.

Never reconstruct unsupported intersections, sum GA4 session estimates, label
all traffic as organic, infer missing rows are zero, or attribute an effect.
"""
from datetime import date, timedelta, timezone
from zoneinfo import ZoneInfo

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import instant
from app.report_v22.comparison_models import ComparisonRequest, ComparisonResult, RULESET
from app.report_v22.comparison_windows import plan_windows
from app.report_v22.first_party_findings import _validate_snapshot
from app.report_v22.implementation_records import implementation_reference

MAX_SAFE = 2**53-1
VIEWS = {"page": "pages", "query": "queries", "device": "devices", "country": "countries"}


def _days(period):
    return [period.start+timedelta(days=i) for i in range((period.end-period.start).days+1)]


def _gsc_dates(value):
    """Defend against duplicate keys and malformed models, not only HTTP normalization."""
    rows = {}
    for period in (value.previous, value.current):
        if period.start_date > period.end_date or (period.end_date-period.start_date).days >= 90:
            raise ValueError("invalid GSC period")
        if period.dates.truncated or period.dates.aggregation_type != "byProperty":
            raise ValueError("daily property aggregation is not trustworthy")
        for row in period.dates.rows:
            if not isinstance(row.key, str):
                raise ValueError("GSC date key is missing")
            key = date.fromisoformat(row.key)
            if key in rows or not period.start_date <= key <= period.end_date or key.isoformat() != row.key:
                raise ValueError("duplicate or out-of-scope GSC date")
            rows[key] = row
    if value.previous.end_date >= value.current.start_date:
        raise ValueError("overlapping GSC periods")
    return rows


def _counts(rows):
    clicks, impressions = 0, 0
    for row in rows:
        if any(not float(v).is_integer() or not 0 <= v <= MAX_SAFE for v in (row.clicks, row.impressions)) or row.clicks > row.impressions:
            raise ValueError("invalid GSC count")
        clicks += int(row.clicks); impressions += int(row.impressions)
    if max(clicks, impressions) > MAX_SAFE:
        raise ValueError("comparison count exceeds shared numeric range")
    return clicks, impressions


def _extract(value, expected, scope):
    rows = _gsc_dates(value)
    if not all(day in rows for day in _days(expected)):
        return None, "DAILY_COVERAGE_INCOMPLETE"
    if scope.dimension == "property":
        return _counts([rows[day] for day in _days(expected)]), None
    view_name = VIEWS.get(scope.dimension)
    if not view_name:
        return None, "DIMENSION_NOT_OBSERVED"
    period = next((p for p in (value.previous, value.current) if p.start_date == expected.start and p.end_date == expected.end), None)
    if period is None:
        return None, "EXACT_WINDOW_AGGREGATE_REQUIRED"
    view = getattr(period, view_name)
    if view.truncated:
        return None, "DETAIL_TRUNCATED"
    if scope.dimension != "page" and view.aggregation_type != "byProperty":
        return None, "AGGREGATION_NOT_SUPPORTED"
    if scope.dimension == "page" and view.aggregation_type != "byPage":
        return None, "AGGREGATION_NOT_SUPPORTED"
    keys = [row.key for row in view.rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate GSC target")
    selected = [row for row in view.rows if row.key == scope.key]
    if not selected:
        return None, "TARGET_NOT_OBSERVED_IN_BOTH_WINDOWS"
    return _counts(selected), None


def _metrics(before, after):
    result = []
    for index, name in enumerate(("clicks", "impressions")):
        a, b = before[index], after[index]
        result.append(dict(metric=name, unit="count", state="comparable", before=a, after=b, absolute_change=b-a,
            relative_change=(b-a)/a if a else None, percentage_point_change=None, reasons=[] if a else ["ZERO_BASELINE_NO_RELATIVE_CHANGE"]))
    a = before[0]/before[1] if before[1] else None
    b = after[0]/after[1] if after[1] else None
    result.append(dict(metric="ctr", unit="ratio", state="comparable" if a is not None and b is not None else "insufficient",
        before=a, after=b, absolute_change=b-a if a is not None and b is not None else None, relative_change=None,
        percentage_point_change=(b-a)*100 if a is not None and b is not None else None,
        reasons=[] if a is not None and b is not None else ["ZERO_IMPRESSIONS_NO_CTR"]))
    return result


def compare_implementation(request: ComparisonRequest) -> ComparisonResult:
    request = ComparisonRequest.model_validate(request)
    entry = next(e for e in request.implementation.entries if e.action_id == request.action_id)
    values = []
    # Reuse persisted checksum, schema, identity/resource/coverage and health checks.
    for source in (request.before, request.after):
        values.append(_validate_snapshot(source) if source else None)
    before, after = values
    source_zone = "America/Los_Angeles" if request.scope.source == "gsc" else entry.timezone
    if request.scope.source == "ga4":
        zones = {getattr(p, name).metadata.time_zone for value in values if value for p in (value.previous, value.current)
            for name in ("totals", "landing_pages", "dates", "key_events")}
        if len(zones) == 1:
            source_zone = next(iter(zones))
    windows = plan_windows(entry, source_zone, request.days)
    cutoffs = []
    for saved, value in zip((request.before, request.after), values):
        cutoff = None
        if saved and value and request.scope.source == "gsc":
            observed = _gsc_dates(value)
            cutoff = min(max(observed), saved.fetched_at.astimezone(ZoneInfo(source_zone)).date()-timedelta(days=1)) if observed else None
        cutoffs.append(cutoff)
    limitations = ["Implementation is user-reported; this comparison does not verify the live page.",
        "Descriptive movement only. Metric-specific low-volume effect eligibility has not been calibrated.",
        "Seasonality, demand, competitors and concurrent changes may affect the observed values. No causal attribution."]
    if len(request.implementation.entries) > 1:
        limitations.append("Multiple actions are recorded; observed movement cannot be isolated to the selected action.")
    limitations.append({"unknown": "Concurrent changes have not been checked.", "reported": "Other concurrent changes were reported and may affect the comparison.",
        "none_reported": "No concurrent changes were reported; this is not independent confirmation that none occurred."}[request.concurrent_changes])
    if request.scope.dimension == "property":
        limitations.append("Property-wide movement is not page-specific evidence or proof of the selected action's effect.")
    for value in values:
        if value:
            limitations.extend(value.limitations)
    def finish(state, reasons, metrics=None):
        def ref(saved, cutoff, period):
            return None if not saved else dict(snapshot_id=str(saved.snapshot_id), binding_id=str(saved.binding_id), payload_checksum=saved.payload_checksum,
                resource_id=saved.external_resource_id, fetched_at=saved.fetched_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), data_cutoff=cutoff.isoformat() if cutoff else None,
                used_through=period.end.isoformat() if state == "comparable" else None)
        body = dict(schema_version="v22_comparison_result_v1", ruleset_version=RULESET,
            input_checksum=verified_request_digest(request.model_dump(mode="json")), implementation=implementation_reference(request.implementation).model_dump(mode="json"),
            action_id=request.action_id, evaluated_at=request.evaluated_at, scope=request.scope.model_dump(mode="json"), windows=windows.model_dump(mode="json"),
            state=state, reasons=list(dict.fromkeys(reasons)), before_source=ref(request.before, cutoffs[0], windows.before), after_source=ref(request.after, cutoffs[1], windows.after), metrics=metrics or [],
            effect_eligibility="pending_metric_specific_calibration", effect_conclusion=None, causal_attribution=False, limitations=list(dict.fromkeys(limitations)))
        body["checksum"] = verified_request_digest(body)
        from app.jobs_v22.digest import verified_canonical_json_bytes
        return ComparisonResult.model_validate_json(verified_canonical_json_bytes(body))
    if not request.before or not request.after:
        return finish("waiting" if instant(request.evaluated_at).astimezone(ZoneInfo(source_zone)).date() <= windows.after.end else "insufficient", ["SNAPSHOT_MISSING"])
    if (request.before.binding_id != request.after.binding_id or request.before.external_resource_id != request.after.external_resource_id):
        return finish("not_comparable", ["SOURCE_BINDING_CHANGED"])
    if any(s.identity_match_status != "matched" for s in (request.before, request.after)):
        return finish("not_comparable", ["IDENTITY_NOT_MATCHED"])
    if any(s.health_status != "healthy" for s in (request.before, request.after)):
        return finish("insufficient", ["SOURCE_UNHEALTHY"])
    if request.after.expires_at <= instant(request.evaluated_at):
        return finish("not_comparable", ["CURRENT_SNAPSHOT_EXPIRED"])
    if request.scope.filters:
        return finish("not_comparable", ["COMBINED_DIMENSIONS_NOT_OBSERVED"])
    if request.scope.source == "ga4":
        if request.scope.channel == "organic_search":
            return finish("not_comparable", ["ORGANIC_CHANNEL_NOT_OBSERVED"])
        if set(before.host_filter) != set(after.host_filter):
            return finish("not_comparable", ["HOST_SCOPE_CHANGED"])
        if len(zones) != 1:
            return finish("not_comparable", ["SOURCE_TIMEZONE_CHANGED"])
        metadata = [getattr(p, name).metadata for value in values for p in (value.previous, value.current)
            for name in ("totals", "landing_pages", "dates", "key_events")]
        if any(m.sampling or m.subject_to_thresholding or m.data_loss_from_other_row or m.metric_restrictions for m in metadata):
            return finish("not_comparable", ["GA4_REPORT_RESTRICTED"])
        if [e.model_dump(mode="json") for e in before.configured_key_events] != [e.model_dump(mode="json") for e in after.configured_key_events]:
            return finish("not_comparable", ["GA4_EVENT_CONFIGURATION_CHANGED"])
        return finish("not_comparable", ["GA4_COMPLETENESS_NOT_ATTESTED", "GA4_EVENT_HISTORY_NOT_VERIFIED"])
    if request.scope.dimension == "landing_page":
        return finish("not_comparable", ["DIMENSION_NOT_OBSERVED"])
    if instant(request.evaluated_at).astimezone(ZoneInfo(source_zone)).date() <= windows.after.end:
        return finish("waiting", ["AFTER_WINDOW_NOT_ELAPSED"])
    if request.after.coverage_end < windows.after.end:
        return finish("waiting", ["AFTER_COLLECTION_NOT_AVAILABLE"])
    if request.before.coverage_start > windows.before.start or request.before.coverage_end < windows.before.end:
        return finish("insufficient", ["BEFORE_WINDOW_NOT_COVERED"])
    if any(cutoff is None or cutoff < period.end for cutoff, period in zip(cutoffs, (windows.before, windows.after))):
        return finish("insufficient", ["FINAL_DATE_COVERAGE_UNCONFIRMED"])
    first, reason_before = _extract(before, windows.before, request.scope)
    second, reason_after = _extract(after, windows.after, request.scope)
    if reason_before or reason_after:
        reasons = [r for r in (reason_before, reason_after) if r]
        return finish("not_comparable" if any(r in {"DIMENSION_NOT_OBSERVED", "AGGREGATION_NOT_SUPPORTED"} for r in reasons) else "insufficient", reasons)
    return finish("comparable", [], _metrics(first, second))
