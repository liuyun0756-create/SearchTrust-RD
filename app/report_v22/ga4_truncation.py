"""Safe, bounded GA4 truncation signals; never persist provider free-text messages."""
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from app.report_v22.comparison_base import StrictModel


class TruncationRange(StrictModel):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("invalid truncation range")
        return self


class TruncationReason(StrictModel):
    # Preserve unknown future enum values rather than treating them as unrestricted.
    type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    date_ranges: list[TruncationRange] = Field(max_length=10)
    before_date: date | None


class TruncationCheck(StrictModel):
    period: Literal["previous", "current"]
    view: Literal["totals", "dates", "key_events"]
    reasons: list[TruncationReason] = Field(max_length=20)


def truncation_check(metadata: dict, *, period: str, view: str) -> TruncationCheck:
    raw = metadata.get("dataTruncationReasons", [])
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError("invalid truncation reasons")
    reasons = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid truncation reason")
        ranges = item.get("dataTruncationDateRanges", [])
        if not isinstance(ranges, list) or len(ranges) > 10 or any(not isinstance(r, dict) for r in ranges):
            raise ValueError("invalid truncation dates")
        # Provider dates must be ISO strings, not coercible timestamps/integers.
        def day(value):
            if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                raise ValueError("invalid provider date")
            return date.fromisoformat(value)
        reasons.append(TruncationReason(type=item.get("dataTruncationType", "DATA_TRUNCATION_TYPE_UNSPECIFIED"),
            date_ranges=[TruncationRange(start=day(r.get("startDate")), end=day(r.get("endDate"))) for r in ranges],
            before_date=day(item["dataTruncationDate"]) if "dataTruncationDate" in item else None))
    return TruncationCheck(period=period, view=view, reasons=reasons)
