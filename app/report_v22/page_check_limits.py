"""Bounded rate metadata; no infrastructure exception details leave this boundary."""
from typing import Literal

from pydantic import Field, model_validator

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import Timestamp, instant
from app.report_v22.page_implementation_models import FrozenModel


class PageCheckLimits(FrozenModel):
    used: int = Field(ge=0, le=2147483647)
    active: bool
    server_time: Timestamp
    next_request_at: Timestamp
    reason: Literal["active", "cooldown", "quota"] | None

    @model_validator(mode="after")
    def consistent(self):
        wait = instant(self.next_request_at) > instant(self.server_time)
        expected = "active" if self.active else "quota" if self.used >= 10 else "cooldown" if wait else None
        if self.reason != expected or instant(self.next_request_at) < instant(self.server_time):
            raise ValueError("inconsistent rate limit")
        if self.used == 0 and wait or self.used >= 10 and not wait:
            raise ValueError("inconsistent quota time")
        return self


class PageCheckRateLimited(DeterministicJobError):
    def __init__(self, limits: PageCheckLimits):
        super().__init__("V22_PAGE_CHECK_RATE_LIMIT", "This Case cannot start another page check yet.")
        self.limits = PageCheckLimits.model_validate_json(limits.model_dump_json())
        if self.limits.reason is None:
            raise ValueError("a rate rejection requires a reason")
