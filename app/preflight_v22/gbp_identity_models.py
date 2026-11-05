"""Explicit v2 internal discovery contract. Legacy preflight remains unchanged.

The caller supplies saved, Case-bound observations, not customer-provided claims.
No operating model or market is inferred from a GBP street address here.
"""
from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from app.report_v22.models import StrictModel, TargetMarket


class IdentityObservation(StrictModel):
    value: str = Field(min_length=1, max_length=1000)
    page_url: HttpUrl
    source_ref: str = Field(min_length=1)
    belongs_to_business: bool = False
    purpose: Literal["business", "competitor", "privacy", "review"] = "business"


class VerifiedAlias(StrictModel):
    url: HttpUrl
    source_ref: str = Field(min_length=1)
    method: Literal["saved_redirect", "seo_confirmation"]


class SavedGbpRecord(StrictModel):
    source_ref: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    payload_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    observed_at: AwareDatetime
    # Preserve the original binding, including absence of a declared expiry.
    # Unavailable/expired inputs are clues only. Never refresh their timestamp.
    expires_at: AwareDatetime | None = None
    available: bool = True
    kind: Literal["market", "detail", "lookup"]
    business_name: str | None = None
    website_url: str | None = None
    phone: str | None = None
    address: str | None = None
    categories: list[str] = Field(default_factory=list)
    cid: str | None = Field(default=None, pattern=r"^[0-9]+$")
    place_id: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def lifetime(self):
        if self.expires_at and not self.observed_at < self.expires_at <= self.observed_at + timedelta(days=30):
            raise ValueError("saved GBP validity must preserve the source lifetime (at most 30 days)")
        return self


class GbpIdentityRequest(StrictModel):
    schema_version: Literal["gbp_identity_request_v2"] = "gbp_identity_request_v2"
    case_ref: str = Field(min_length=1)
    site_url: HttpUrl
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    target_market: TargetMarket
    evaluated_at: AwareDatetime
    saved_records: list[SavedGbpRecord] = Field(default_factory=list)
    phones: list[IdentityObservation] = Field(default_factory=list)
    names: list[IdentityObservation] = Field(default_factory=list)
    aliases: list[VerifiedAlias] = Field(default_factory=list)
    gbp_url: str | None = None
    # Previously resolved links only: this path never follows a URL itself.
    saved_link_destinations: dict[str, str] = Field(default_factory=dict)
    selected_branch_ref: str | None = None
    selection_source_ref: str | None = None
    seo_business_name: str | None = None
    seo_name_source_ref: str | None = None
    market_collected: bool = False

    @model_validator(mode="after")
    def provenance(self):
        if bool(self.selected_branch_ref) != bool(self.selection_source_ref):
            raise ValueError("branch selection needs explicit confirmation provenance")
        if bool(self.seo_business_name) != bool(self.seo_name_source_ref):
            raise ValueError("SEO name needs explicit provenance")
        if any(record.observed_at > self.evaluated_at for record in self.saved_records):
            raise ValueError("saved observation is from the future")
        return self


class CandidateDecision(StrictModel):
    record: SavedGbpRecord
    domain_match: bool | None
    phone_match: bool | None
    eligible: bool
    current: bool
    reasons: list[str]
    matched_source_refs: list[str] = Field(default_factory=list)


class LookupAttempt(StrictModel):
    number: int = Field(ge=1, le=3)
    stage: Literal["link", "refresh", "domain_phone", "name"]
    outcome: Literal["started", "success", "failed", "interrupted"] = "started"


class GbpLookupLedger(StrictModel):
    """Return and persist with the Case operation before retry/resume; never reset."""
    case_ref: str
    normalized_domain: str
    attempts: list[LookupAttempt] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def consecutive(self):
        if [a.number for a in self.attempts] != list(range(1, len(self.attempts) + 1)):
            raise ValueError("attempt ledger must be consecutive")
        return self


class GbpIdentityResult(StrictModel):
    schema_version: Literal["gbp_identity_result_v2"] = "gbp_identity_result_v2"
    status: Literal["matched", "identity_unconfirmed", "gbp_not_found", "lookup_not_checked"]
    reason: str
    lookup_stop_reason: str | None = None
    selected: SavedGbpRecord | None = None
    candidates: list[CandidateDecision]
    ledger: GbpLookupLedger
    visited_stages: list[str]

    @model_validator(mode="after")
    def positive_match_only(self):
        if (self.status == "matched") != (self.selected is not None):
            raise ValueError("only matched results have a selected business")
        if self.selected and not any(c.record == self.selected and c.eligible and c.current for c in self.candidates):
            raise ValueError("selected business requires current D/P-positive evidence")
        return self
