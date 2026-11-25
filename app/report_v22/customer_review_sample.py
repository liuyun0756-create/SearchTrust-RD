"""Small, frozen review observations. No reviewer identity or review text."""
from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.report_v22.models import StrictModel

RULESET = "v22_capability_public_v22"
MANIFEST_SCHEMA = "v22_analysis_input_v12"
REPORT_SCHEMA = "2.2.17"
SNAPSHOT_SCHEMA = "customer_public_gbp_snapshot_v3"
BASE_RULESETS = frozenset(f"v22_capability_public_v{v}" for v in (15, 16, 17, 18, 20))
PAGE_LIMIT = 4
SAMPLE_LIMIT = 30


class CustomerReview(StrictModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: bool
    iso_date: AwareDatetime | None
    reply_state: Literal["visible", "not_visible", "unknown"]


class CustomerReviewPage(StrictModel):
    sampled_at: AwareDatetime
    response_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sort_verified: bool
    has_next: bool
    returned: int = Field(ge=0, le=100)


class CustomerReviewSample(StrictModel):
    branch_ref: str = Field(min_length=1, max_length=480)
    data_id: str | None = Field(pattern=r"^0x[0-9a-fA-F]+:0x[0-9a-fA-F]+$", max_length=200)
    pages: list[CustomerReviewPage] = Field(max_length=PAGE_LIMIT)
    reviews: list[CustomerReview] = Field(max_length=SAMPLE_LIMIT)
    sampled_at: AwareDatetime | None
    completed_at: AwareDatetime
    stop: Literal["natural_end", "sample_cap", "page_cap", "provider_failure", "branch_unavailable", "invalid_pagination", "budget"]
    invalid_records: int = Field(ge=0, le=400)
    discarded_recent_or_invalid: bool

    @model_validator(mode="after")
    def coherent(self):
        if (self.sampled_at != (self.pages[0].sampled_at if self.pages else None)
                or (self.pages and self.data_id is None)
                or any(not p.has_next for p in self.pages[:-1])
                or any(p.sampled_at > self.completed_at for p in self.pages)
                or len(self.reviews) > sum(p.returned for p in self.pages)
                or len({r.id for r in self.reviews}) != len(self.reviews)
                or (self.stop == "natural_end" and (not self.pages or self.pages[-1].has_next))
                or (self.stop == "sample_cap" and len(self.reviews) != SAMPLE_LIMIT)
                or (self.stop == "page_cap" and len(self.pages) != PAGE_LIMIT)):
            raise ValueError("incoherent customer review sampling")
        return self

    @property
    def exact_window(self) -> bool:
        if not self.reviews or self.sampled_at is None:
            return False
        dates = [r.iso_date for r in self.reviews]
        if (self.stop not in {"natural_end", "sample_cap", "page_cap"}
                or self.invalid_records or self.discarded_recent_or_invalid
                or not all(p.sort_verified for p in self.pages)
                or not all(r.provider_id for r in self.reviews)
                or any(t is None or t > self.sampled_at for t in dates)
                or any(a < b for a, b in zip(dates, dates[1:]))):
            return False
        crossed_window = dates[-1] < self.sampled_at - timedelta(days=90)
        return crossed_window or (self.stop == "natural_end" and len(self.reviews) < SAMPLE_LIMIT)
