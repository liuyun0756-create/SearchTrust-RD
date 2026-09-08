"""Window compatibility, complete-week alignment and dependency-free Spearman."""

from __future__ import annotations

from datetime import date, timedelta
import math
from typing import Mapping


def windows_compatible(
    left_start: date, left_end: date, right_start: date, right_end: date,
    *, maximum_end_delta_days: int = 3,
) -> bool:
    return (
        (left_end - left_start).days == 89
        and (right_end - right_start).days == 89
        and abs((left_end - right_end).days) <= maximum_end_delta_days
    )


def complete_week_pairs(
    left: Mapping[date, float], right: Mapping[date, float],
    *, start: date, end: date, right_lag_days: int = 2,
) -> list[tuple[date, float, float]]:
    effective_end = end - timedelta(days=right_lag_days)
    first = start + timedelta(days=(-start.weekday()) % 7)
    last = effective_end - timedelta(days=(effective_end.weekday() + 1) % 7)
    result: list[tuple[date, float, float]] = []
    cursor = first
    while cursor + timedelta(days=6) <= last:
        dates = [cursor + timedelta(days=offset) for offset in range(7)]
        if all(day in left and day in right for day in dates):
            result.append((cursor, sum(left[day] for day in dates), sum(right[day] for day in dates)))
        cursor += timedelta(days=7)
    return result


def _average_ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        rank = (cursor + 1 + end) / 2
        for original, _ in indexed[cursor:end]:
            result[original] = rank
        cursor = end
    return result


def spearman(values: list[tuple[date, float, float]], *, minimum_weeks: int = 8) -> float | None:
    if len(values) < minimum_weeks:
        return None
    left = [item[1] for item in values]
    right = [item[2] for item in values]
    if len(set(left)) < 2 or len(set(right)) < 2:
        return None
    left_ranks, right_ranks = _average_ranks(left), _average_ranks(right)
    left_mean = sum(left_ranks) / len(left_ranks)
    right_mean = sum(right_ranks) / len(right_ranks)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left_ranks, right_ranks))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left_ranks)
        * sum((b - right_mean) ** 2 for b in right_ranks)
    )
    return numerator / denominator if denominator else None
