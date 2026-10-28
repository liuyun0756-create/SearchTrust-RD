from copy import deepcopy
from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.comparison import _metrics, compare_implementation
from app.report_v22.comparison_windows import plan_windows
from app.report_v22.first_party_findings_errors import FirstPartyFindingsError
from app.report_v22.implementation_records import ImplementationEntry
from tests.support.comparison_fixtures import ga4_request_raw, parsed, request_raw, resign_source, saved_gsc


def entry(day="2026-09-01", zone="America/Los_Angeles"):
    return ImplementationEntry(action_id="a", scope="The service page", implemented_on=day, timezone=zone, note=None, responsible=None)


@pytest.mark.parametrize("days", [7, 14, 28, 90])
def test_equal_windows_exclude_the_entire_implementation_day(days):
    w = plan_windows(entry(), "America/Los_Angeles", days)
    assert w.excluded.start == w.excluded.end == date(2026, 9, 1)
    assert w.before.end == date(2026, 8, 31) and w.after.start == date(2026, 9, 2)
    assert (w.before.end-w.before.start).days == (w.after.end-w.after.start).days == days-1


@pytest.mark.parametrize("day", ["2026-03-08", "2026-11-01", "2024-02-29", "2026-01-01"])
def test_dst_leap_and_year_boundaries_count_calendar_days(day):
    w = plan_windows(entry(day), "America/Los_Angeles")
    assert w.excluded.start == w.excluded.end == date.fromisoformat(day)
    assert (w.after.end-w.after.start).days == 27


def test_different_timezones_exclude_both_overlapping_source_dates():
    w = plan_windows(entry("2026-09-01", "Asia/Shanghai"), "America/Los_Angeles")
    assert (w.excluded.start, w.excluded.end) == (date(2026, 8, 31), date(2026, 9, 1))
    assert w.before.end == date(2026, 8, 30) and w.after.start == date(2026, 9, 2)


def test_nonexistent_local_day_and_unsupported_lengths_are_not_guessed():
    with pytest.raises(ValueError): plan_windows(entry("2011-12-30", "Pacific/Apia"), "UTC")
    for days in (True, 0, 27, 28.0):
        with pytest.raises(ValueError): plan_windows(entry(), "UTC", days)


@pytest.mark.parametrize("direction,clicks,impressions,change", [("up", 12, 240, 56), ("down", 8, 160, -56), ("same", 10, 200, 0)])
def test_actual_saved_daily_gsc_windows_describe_movement_without_effect(direction, clicks, impressions, change):
    raw = request_raw()
    for period in ("previous", "current"):
        for row in raw["after"]["normalized_payload"][period]["dates"]["rows"]:
            if row["key"] >= "2026-09-02": row.update(clicks=clicks, impressions=impressions, ctr=clicks/impressions)
    resign_source(raw["after"])
    original = deepcopy(raw)
    result = compare_implementation(parsed(raw))
    assert result.state == "comparable" and result.metrics[0].before == 280 and result.metrics[0].absolute_change == change
    assert result.metrics[0].relative_change == pytest.approx(change/280)
    assert result.metrics[2].percentage_point_change == pytest.approx(0)
    assert result.effect_conclusion is None and result.causal_attribution is False
    assert result.effect_eligibility == "pending_metric_specific_calibration"
    assert result.before_source.data_cutoff == date(2026, 8, 31)
    assert result.after_source.data_cutoff == date(2026, 10, 19)
    assert result.after_source.used_through == date(2026, 9, 29)
    assert result.checksum == verified_request_digest(result.model_dump(mode="json", exclude={"checksum"}))
    assert result == compare_implementation(parsed(raw)) and raw == original


def test_zero_baseline_and_zero_impressions_have_no_meaningless_percentage():
    values = _metrics((0, 100), (4, 100))
    assert values[0]["relative_change"] is None and values[0]["absolute_change"] == 4
    assert values[2]["percentage_point_change"] == pytest.approx(4)
    zero = _metrics((0, 0), (0, 100))
    assert zero[2]["state"] == "insufficient" and zero[2]["before"] is None and zero[2]["absolute_change"] is None


def test_ctr_uses_counts_not_the_mean_of_daily_rates():
    raw = request_raw()
    for period in ("previous", "current"):
        for i, row in enumerate(raw["after"]["normalized_payload"][period]["dates"]["rows"]):
            row.update(clicks=10, impressions=100 if i % 2 else 900)
            row["ctr"] = row["clicks"]/row["impressions"]
    resign_source(raw["after"])
    result = compare_implementation(parsed(raw))
    assert result.metrics[2].after == pytest.approx(.02)


@pytest.mark.parametrize("change,code", [("resource", "SOURCE_BINDING_CHANGED"), ("binding", "SOURCE_BINDING_CHANGED"), ("expired", "CURRENT_SNAPSHOT_EXPIRED"),
    ("filter", "COMBINED_DIMENSIONS_NOT_OBSERVED"), ("page", "EXACT_WINDOW_AGGREGATE_REQUIRED"), ("date", "DAILY_COVERAGE_INCOMPLETE")])
def test_incompatible_or_incomplete_sources_do_not_produce_deltas(change, code):
    raw = request_raw()
    if change == "resource":
        raw["after"]["external_resource_id"] = raw["after"]["normalized_payload"]["resource_id"] = "sc-domain:other.test"; resign_source(raw["after"])
    if change == "binding": raw["after"]["binding_id"] = str(uuid4())
    if change == "expired": raw["after"]["expires_at"] = "2026-10-22T07:00:00Z"
    if change == "filter": raw["scope"]["filters"] = [dict(dimension="device", value="MOBILE")]
    if change == "page": raw["scope"].update(dimension="page", key="https://example.test/service")
    if change == "date":
        rows = raw["after"]["normalized_payload"]["current"]["dates"]["rows"]
        rows[:] = [r for r in rows if r["key"] != "2026-09-10"]; resign_source(raw["after"])
    result = compare_implementation(parsed(raw))
    assert result.state in {"not_comparable", "insufficient"} and code in result.reasons and result.metrics == []
    assert result.effect_conclusion is None


def test_historical_expiry_does_not_rewrite_the_baseline_but_current_expiry_blocks():
    raw = request_raw()
    assert raw["before"]["expires_at"] < raw["evaluated_at"]
    assert compare_implementation(parsed(raw)).state == "comparable"


def test_implementation_revision_and_multiple_actions_remain_visible_limits():
    raw = request_raw()
    other = deepcopy(raw["implementation"]["entries"][0]); other["action_id"] = "another-saved-action"
    raw["implementation"]["entries"].append(other)
    raw["implementation"]["checksum"] = verified_request_digest({k:v for k,v in raw["implementation"].items() if k != "checksum"})
    result = compare_implementation(parsed(raw))
    assert result.implementation.checksum == raw["implementation"]["checksum"]
    assert any("Multiple actions" in limitation for limitation in result.limitations)
    assert any("Property-wide" in limitation for limitation in result.limitations)


@pytest.mark.parametrize("change", ["case", "checksum", "health", "coverage", "duplicate-date", "future-fetch", "implementation", "missing-action"])
def test_invalid_frozen_inputs_raise_instead_of_becoming_normal_insufficiency(change):
    raw = request_raw()
    if change == "case": raw["after"]["case_id"] = str(uuid4())
    if change == "checksum": raw["after"]["payload_checksum"] = "sha256:"+"0"*64
    if change == "health": raw["after"]["health_status"] = "unhealthy"
    if change == "coverage": raw["after"]["coverage_start"] = "2026-01-01"
    if change == "future-fetch": raw["after"]["fetched_at"] = "2026-10-23T08:00:00Z"
    if change == "implementation": raw["implementation"]["entries"][0]["implemented_on"] = "2026-09-02"
    if change == "missing-action": raw["action_id"] = "not-saved"
    if change == "duplicate-date":
        rows = raw["after"]["normalized_payload"]["current"]["dates"]["rows"]; rows.append(deepcopy(rows[0])); resign_source(raw["after"])
    with pytest.raises((ValueError, FirstPartyFindingsError)): compare_implementation(parsed(raw))


def test_waiting_requires_a_full_after_window_and_never_starts_analysis():
    raw = request_raw()
    raw["implementation"]["entries"][0]["implemented_on"] = "2026-10-20"
    raw["implementation"]["checksum"] = verified_request_digest({k:v for k,v in raw["implementation"].items() if k != "checksum"})
    result = compare_implementation(parsed(raw))
    assert result.state == "waiting" and "AFTER_WINDOW_NOT_ELAPSED" in result.reasons and result.metrics == []
    raw["after"] = None
    assert compare_implementation(parsed(raw)).state == "waiting"


def test_exact_90_day_page_windows_can_be_compared_but_never_sliced_from_aggregates():
    from uuid import UUID
    raw = request_raw()
    raw.update(days=90, evaluated_at="2026-12-03T08:00:00Z")
    raw["after"] = saved_gsc(UUID(raw["implementation"]["case_id"]), after=True, end=date(2026, 11, 30)).model_dump(mode="json")
    raw["scope"].update(dimension="page", key="https://example.test/service")
    result = compare_implementation(parsed(raw))
    assert result.state == "comparable" and result.metrics[0].before == 900 and result.metrics[0].after == 1080
    raw["after"]["normalized_payload"]["current"]["pages"]["aggregation_type"] = "auto"
    resign_source(raw["after"])
    assert "AGGREGATION_NOT_SUPPORTED" in compare_implementation(parsed(raw)).reasons


@pytest.mark.parametrize("change,code", [("organic", "ORGANIC_CHANNEL_NOT_OBSERVED"), ("none", "GA4_COMPLETENESS_NOT_ATTESTED"),
    ("host", "HOST_SCOPE_CHANGED"), ("zone", "SOURCE_TIMEZONE_CHANGED"), ("sampling", "GA4_REPORT_RESTRICTED"),
    ("threshold", "GA4_REPORT_RESTRICTED"), ("other", "GA4_REPORT_RESTRICTED"), ("event", "GA4_EVENT_CONFIGURATION_CHANGED")])
def test_ga4_all_traffic_approximation_or_missing_finality_cannot_become_an_organic_effect(change, code):
    raw = ga4_request_raw()
    value = raw["after"]["normalized_payload"]
    if change == "organic": raw["scope"]["channel"] = "organic_search"
    if change == "host": value["host_filter"] = ["other.test"]
    if change == "zone":
        for p in (value["previous"], value["current"]):
            for name in ("totals", "dates", "landing_pages", "key_events"): p[name]["metadata"]["time_zone"] = "Europe/London"
    if change == "sampling": value["current"]["dates"]["metadata"]["sampling"] = [dict(samples_read_count=10, sampling_space_size=20)]
    if change == "threshold": value["current"]["dates"]["metadata"]["subject_to_thresholding"] = True
    if change == "other": value["current"]["dates"]["metadata"]["data_loss_from_other_row"] = True
    if change == "event": value["configured_key_events"][0]["counting_method"] = "ONCE_PER_SESSION"
    resign_source(raw["after"])
    result = compare_implementation(parsed(raw))
    assert result.state == "not_comparable" and code in result.reasons and not result.metrics
    assert result.effect_conclusion is None and result.after_source.data_cutoff is None


@pytest.mark.parametrize("change,code,state", [("before-missing", "SNAPSHOT_MISSING", "insufficient"),
    ("collection-pending", "AFTER_COLLECTION_NOT_AVAILABLE", "waiting"), ("identity", "IDENTITY_NOT_MATCHED", "not_comparable"),
    ("unhealthy", "SOURCE_UNHEALTHY", "insufficient"), ("unsupported-dimension", "DIMENSION_NOT_OBSERVED", "not_comparable")])
def test_qualification_states_are_explicit_and_never_mean_no_effect(change, code, state):
    raw = request_raw()
    if change == "before-missing": raw["before"] = None
    if change == "identity": raw["after"]["identity_match_status"] = "mismatch"
    if change == "unsupported-dimension": raw["scope"].update(dimension="landing_page", key="/service")
    if change == "collection-pending":
        # Window ends before evaluation, but beyond the saved collection cutoff.
        raw["implementation"]["entries"][0]["implemented_on"] = "2026-09-22"
        raw["implementation"]["checksum"] = verified_request_digest({k:v for k,v in raw["implementation"].items() if k != "checksum"})
    if change == "unhealthy":
        value = raw["after"]["normalized_payload"]; value["current"]["totals"]["rows"] = []; resign_source(raw["after"])
    result = compare_implementation(parsed(raw))
    assert result.state == state and code in result.reasons and not result.metrics and result.effect_conclusion is None
    assert not result.after_source or result.after_source.used_through is None


def test_partial_daily_rows_and_same_snapshot_reuse_preserve_complete_scope():
    raw = request_raw()
    raw["before"] = deepcopy(raw["after"])
    result = compare_implementation(parsed(raw))
    assert result.state == "comparable" and result.before_source.snapshot_id == result.after_source.snapshot_id
    raw["scope"]["filters"] = [dict(dimension="query", value="repair service")]
    result = compare_implementation(parsed(raw))
    assert result.state == "not_comparable" and result.metrics == []


def test_missing_target_url_and_truncation_never_turn_into_a_zero_baseline():
    from uuid import UUID
    raw = request_raw()
    raw.update(days=90, evaluated_at="2026-12-03T08:00:00Z")
    raw["after"] = saved_gsc(UUID(raw["implementation"]["case_id"]), after=True, end=date(2026, 11, 30)).model_dump(mode="json")
    raw["scope"].update(dimension="page", key="https://example.test/changed-url")
    result = compare_implementation(parsed(raw))
    assert result.state == "insufficient" and "TARGET_NOT_OBSERVED_IN_BOTH_WINDOWS" in result.reasons and not result.metrics
    raw["scope"]["key"] = "https://example.test/service"
    raw["after"]["normalized_payload"]["current"]["pages"]["truncated"] = True
    resign_source(raw["after"])
    result = compare_implementation(parsed(raw))
    assert result.state == "insufficient" and "DETAIL_TRUNCATED" in result.reasons and not result.metrics
