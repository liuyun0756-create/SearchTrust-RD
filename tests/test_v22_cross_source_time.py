from datetime import date, timedelta

import pytest

from app.report_v22.cross_source_time import complete_week_pairs, spearman, windows_compatible


def test_window_end_tolerance_is_inclusive_at_three_days() -> None:
    start = date(2026, 1, 1)
    end = start + timedelta(days=89)
    assert windows_compatible(start, end, start + timedelta(days=3), end + timedelta(days=3))
    assert not windows_compatible(start, end, start + timedelta(days=4), end + timedelta(days=4))


def test_complete_weeks_exclude_ga4_lag_and_never_fill_missing_days() -> None:
    start, end = date(2026, 1, 5), date(2026, 3, 8)
    days = {start + timedelta(days=i): 1.0 for i in range((end - start).days + 1)}
    assert len(complete_week_pairs(days, days, start=start, end=end)) == 8
    missing = dict(days)
    missing.pop(date(2026, 1, 7))
    assert len(complete_week_pairs(days, missing, start=start, end=end)) == 7


def test_spearman_uses_average_ranks_and_requires_variance_and_eight_weeks() -> None:
    first = date(2026, 1, 5)
    positive = [(first + timedelta(days=7 * i), value, value * 3) for i, value in enumerate([1, 2, 2, 4, 5, 6, 7, 8])]
    negative = [(day, left, -right) for day, left, right in positive]
    assert spearman(positive) == pytest.approx(1)
    assert spearman(negative) == pytest.approx(-1)
    assert spearman(positive[:7]) is None
    assert spearman([(day, 1, right) for day, _, right in positive]) is None
