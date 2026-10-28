"""Internal, immutable static-page observations. No authority or effect is inferred."""
from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import Checksum, StrictModel, Timestamp, instant
from app.security_v22.urls import normalize_site_url


def clean_text(value: str) -> str:
    if not value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("nonempty text without control characters required")
    return value


def page_url(value: str, *, site: str | None = None) -> str:
    """Do not collapse paths, remove meaningful queries, or infer a destination."""
    clean_text(value)
    if len(value) > 2000 or re.search(r"\s", value) or "\\" in value:
        raise ValueError("unsupported page URL")
    parsed = urlsplit(value)
    root = normalize_site_url(value)  # Includes literal-IP and credentials checks.
    if parsed.scheme != "https" or parsed.port not in (None, 443) or "?" in value:
        raise ValueError("HTTPS default-port query-free page required")
    decoded = unquote(parsed.path)
    if "\\" in decoded or any(unicodedata.category(c).startswith("C") for c in unquote(value)):
        raise ValueError("unsafe encoded URL")
    if any(part in {".", ".."} for part in decoded.split("/")) or re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("ambiguous URL path")
    # Only the exact confirmed host; do not add an implicit www alias.
    if site is not None and urlsplit(root).hostname != urlsplit(normalize_site_url(site)).hostname:
        raise ValueError("page outside confirmed website")
    return urlunsplit(("https", urlsplit(root).netloc, parsed.path or "/", "", parsed.fragment))


class FrozenModel(StrictModel):
    model_config = ConfigDict(frozen=True)


class PageCondition(FrozenModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    kind: Literal["text_present", "link_present", "anchor_present"]
    value: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def valid_value(self):
        clean_text(self.value)
        if len(self.value) > {"text_present": 200, "anchor_present": 128, "link_present": 2000}[self.kind]:
            raise ValueError("condition exceeds limit")
        if self.kind == "link_present" and self.value != page_url(self.value):
            raise ValueError("normalized absolute link required")
        return self


class PageCheckBinding(FrozenModel):
    user_id: UUID
    case_id: UUID
    report_id: UUID
    report_checksum: Checksum
    delivery_revision_id: UUID
    delivery_revision: int = Field(ge=1)
    implementation_id: UUID
    implementation_revision: int = Field(ge=1)
    implementation_checksum: Checksum
    implementation_recorded_at: Timestamp
    action_id: str = Field(min_length=1, max_length=200)
    business_id: UUID
    business_revision: int = Field(ge=1)
    business_checksum: Checksum
    site_url: str = Field(max_length=2000)

    @model_validator(mode="after")
    def valid_binding(self):
        clean_text(self.action_id)
        if self.site_url != normalize_site_url(page_url(self.site_url)):
            raise ValueError("normalized site root required")
        instant(self.implementation_recorded_at)
        return self


Conditions = Annotated[tuple[PageCondition, ...], Field(min_length=1, max_length=5, strict=False)]


class PageCheckPlan(FrozenModel):
    schema_version: Literal["v22_page_implementation_plan_v1"]
    rules_version: Literal["v22_static_page_check_v1"]
    binding: PageCheckBinding
    target_url: str = Field(max_length=2000)
    conditions: Conditions
    checksum: Checksum

    @model_validator(mode="after")
    def valid_plan(self):
        if self.target_url != page_url(self.target_url, site=self.binding.site_url):
            raise ValueError("normalized target required")
        if len({c.id for c in self.conditions}) != len(self.conditions):
            raise ValueError("duplicate condition IDs")
        for condition in self.conditions:
            if condition.kind == "link_present":
                page_url(condition.value, site=self.binding.site_url)
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("plan checksum mismatch")
        return self


Failure = Literal[
    "unsafe_target", "dns_unavailable", "redirect_requires_confirmation", "robots_unavailable",
    "robots_disallowed", "robots_invalid", "crawl_delay_exceeds_budget", "page_timeout",
    "http_error", "http_status", "content_unsupported", "response_too_large", "empty_page",
    "access_restricted", "parse_failed",
]
State = Literal["observed", "not_observed", "unable_to_check"]


class PageConditionResult(FrozenModel):
    condition_id: str = Field(min_length=1, max_length=64)
    state: State


class PageObservation(FrozenModel):
    schema_version: Literal["v22_page_implementation_observation_v1"]
    id: UUID
    plan: PageCheckPlan
    observed_at: Timestamp
    status: Literal["checked", "unable_to_check"]
    failure: Failure | None
    failure_stage: Literal["robots", "page", "check"] | None
    http_status: int | None = Field(ge=100, le=599)
    media_type: Literal["text/html", "application/xhtml+xml"] | None
    response_bytes: int | None = Field(ge=0, le=1048576)
    content_checksum: Checksum | None
    results: Annotated[tuple[PageConditionResult, ...], Field(min_length=1, max_length=5, strict=False)]
    limitation: Literal["static_response_only_rendering_functionality_and_identity_not_verified"]
    effect_conclusion: None
    causal_eligible: Literal[False]
    checksum: Checksum

    @model_validator(mode="after")
    def consistent(self):
        if instant(self.observed_at) < instant(self.plan.binding.implementation_recorded_at):
            raise ValueError("observation predates implementation record")
        if tuple(r.condition_id for r in self.results) != tuple(c.id for c in self.plan.conditions):
            raise ValueError("condition results mismatch")
        if self.status == "checked":
            if (self.failure is not None or self.failure_stage is not None or self.http_status != 200
                    or self.media_type is None or not self.response_bytes or self.content_checksum is None
                    or any(r.state == "unable_to_check" for r in self.results)):
                raise ValueError("invalid successful observation")
        elif (self.failure is None or self.failure_stage is None or self.content_checksum is not None
              or any(r.state != "unable_to_check" for r in self.results)):
            raise ValueError("invalid failed observation")
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("observation checksum mismatch")
        return self


class ConditionChange(FrozenModel):
    condition_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    change: Literal["newly_observed", "no_longer_observed", "unchanged"]


class PageObservationComparison(FrozenModel):
    status: Literal["first_observation", "not_comparable", "compared"]
    reason: Literal["no_baseline", "invalid_baseline", "different_plan", "invalid_time_order", "check_unavailable"] | None
    previous_id: UUID | None
    current_id: UUID
    changes: tuple[ConditionChange, ...] = Field(strict=False, max_length=5)
    effect_conclusion: None = None
    causal_eligible: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "compared":
            if self.reason is not None or self.previous_id is None or self.current_id == self.previous_id or not self.changes:
                raise ValueError("comparison requires two observations and changes")
        elif self.changes or self.reason is None:
            raise ValueError("unavailable comparison must explain why without changes")
        if self.status == "first_observation" and (self.previous_id is not None or self.reason != "no_baseline"):
            raise ValueError("first observation cannot have a baseline")
        if self.status == "not_comparable" and self.reason == "no_baseline":
            raise ValueError("missing baseline is a first observation")
        if len({c.condition_id for c in self.changes}) != len(self.changes):
            raise ValueError("duplicate condition changes")
        return self
