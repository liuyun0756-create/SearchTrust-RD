"""Equal complete calendar windows around a date-only implementation assertion."""
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import model_validator

from app.report_v22.capability import Label
from app.report_v22.comparison_base import StrictModel
from app.report_v22.implementation_records import ImplementationEntry

WindowDays = Literal[7, 14, 28, 90]


class DateRange(StrictModel):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self):
        if self.start > self.end:
            raise ValueError("reversed window")
        return self


class ComparisonWindows(StrictModel):
    days: WindowDays
    timezone: Label
    before: DateRange
    excluded: DateRange
    after: DateRange

    @model_validator(mode="after")
    def consistent(self):
        ZoneInfo(self.timezone)
        if any((period.end-period.start).days+1 != self.days for period in (self.before, self.after)):
            raise ValueError("comparison windows must have equal complete day counts")
        if self.before.end+timedelta(days=1) != self.excluded.start or self.excluded.end+timedelta(days=1) != self.after.start:
            raise ValueError("implementation calendar dates must be excluded")
        return self


def plan_windows(entry: ImplementationEntry, source_timezone: str, days: WindowDays = 28) -> ComparisonWindows:
    entry = ImplementationEntry.model_validate(entry)
    if type(days) is not int or days not in (7, 14, 28, 90):
        raise ValueError("unsupported window length")
    source = ZoneInfo(source_timezone)
    own = ZoneInfo(entry.timezone)
    implemented = date.fromisoformat(entry.implemented_on)
    first = datetime.combine(implemented, time.min, own)
    last = datetime.combine(implemented+timedelta(days=1), time.min, own)
    first_utc, last_utc = first.astimezone(timezone.utc), last.astimezone(timezone.utc)
    if first_utc.astimezone(own).date() != implemented or last_utc <= first_utc:
        raise ValueError("implementation local date does not exist")
    excluded_start = first_utc.astimezone(source).date()
    excluded_end = (last_utc-timedelta(microseconds=1)).astimezone(source).date()
    before_end, after_start = excluded_start-timedelta(days=1), excluded_end+timedelta(days=1)
    return ComparisonWindows(days=days, timezone=source_timezone,
        before=DateRange(start=before_end-timedelta(days=days-1), end=before_end),
        excluded=DateRange(start=excluded_start, end=excluded_end),
        after=DateRange(start=after_start, end=after_start+timedelta(days=days-1)))
