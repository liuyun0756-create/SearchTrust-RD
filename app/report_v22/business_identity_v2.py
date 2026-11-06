"""Explicit v13 identity. Locations are observations, never sampling coordinates.

Recognition and human confirmation are separate. This module performs no I/O.
"""
from typing import Literal
from pydantic import Field, HttpUrl, model_validator, field_validator, model_serializer
from app.report_v22.models import StrictModel, TargetMarket, BusinessIdentity

IDENTITY_RULESET = "v22_capability_public_v13"
OperatingMode = Literal["storefront", "service_area", "hybrid", "meeting_point"]


class ModeEvidence(StrictModel):
    kind: Literal["visiting", "onsite", "meeting", "auxiliary", "gbp_service_area"]
    quote: str = Field(min_length=1, max_length=2000)
    url: HttpUrl
    selector: str = Field(min_length=1, max_length=500)
    payload_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ModeConfirmation(StrictModel):
    value: OperatingMode
    confirmed_by: str = Field(min_length=1, max_length=240)
    confirmed_at: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2000)
    normalized_domain: str = Field(min_length=1)
    selected_branch_ref: str | None
    evidence_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_ref: str = Field(min_length=1, max_length=1000)

    @field_validator("confirmed_at")
    @classmethod
    def timestamp(cls, value):
        from datetime import datetime
        if not value.endswith("Z") or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("confirmation timestamp must be UTC")
        return value


class OperatingModeAssessment(StrictModel):
    value: OperatingMode | None
    state: Literal["resolved", "needs_confirmation", "unresolved"]
    suggested_value: OperatingMode | None
    alternatives: list[OperatingMode] = Field(max_length=4)
    source: Literal["explicit_confirmation", "website", "gbp", "insufficient_evidence"]
    evidence_refs: list[ModeEvidence] = Field(max_length=100)
    conflicts: list[str] = Field(max_length=20)
    confirmation_ref: ModeConfirmation | None = None

    @model_validator(mode="after")
    def coherent(self):
        if (self.state == "resolved") != (self.value is not None):
            raise ValueError("resolved mode requires an effective value")
        if (self.source == "explicit_confirmation") != (self.confirmation_ref is not None):
            raise ValueError("confirmation provenance required")
        if self.confirmation_ref and self.value != self.confirmation_ref.value:
            raise ValueError("confirmed mode mismatch")
        if self.value and not self.confirmation_ref and not self.evidence_refs:
            raise ValueError("automatic resolution requires evidence")
        return self


class OperatingPlace(StrictModel):
    address: str = Field(min_length=1, max_length=1000)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None


class LocationConfirmation(StrictModel):
    confirmed_by: str = Field(min_length=1, max_length=240)
    confirmed_at: str = Field(min_length=1)
    selected_branch_ref: str = Field(min_length=1, max_length=320)
    address: str = Field(min_length=1, max_length=1000)
    source_ref: str = Field(min_length=1, max_length=1000)

    @field_validator("confirmed_at")
    @classmethod
    def timestamp(cls, value):
        return ModeConfirmation.timestamp(value)


class OperatingLocation(StrictModel):
    state: Literal["observed", "seo_confirmed", "not_checked"]
    location: OperatingPlace | None
    role: Literal["customer_facing", "office", "registered", "unknown"]
    source_refs: list[str] = Field(max_length=20)
    confirmation_ref: LocationConfirmation | None = None

    @model_serializer(mode="wrap")
    def preserve_saved_locations(self, handler):
        result = handler(self)
        if self.confirmation_ref is None:
            result.pop("confirmation_ref", None)
        return result

    @model_validator(mode="after")
    def observed(self):
        if (self.state == "not_checked") != (self.location is None):
            raise ValueError("unobserved location must remain empty")
        if self.location and not self.source_refs:
            raise ValueError("observed location needs provenance")
        if self.confirmation_ref and (self.state != "seo_confirmed" or self.role != "customer_facing"
                or self.location is None or self.confirmation_ref.address != self.location.address):
            raise ValueError("address confirmation does not match operating location")
        return self


class IdentityMatch(StrictModel):
    state: Literal["matched", "unresolved", "not_found", "not_checked"]
    matched_on: list[Literal["domain", "phone", "reviewed_saved_identity"]] = Field(max_length=3)
    source_refs: list[str] = Field(max_length=20)
    rejected_candidates: list[str] = Field(max_length=20)


class BusinessIdentityV2(StrictModel):
    schema_version: Literal["business_identity_v2"]
    business_name: str = Field(min_length=1, max_length=240)
    site_url: HttpUrl
    normalized_domain: str = Field(min_length=1, max_length=253)
    public_gbp_url: HttpUrl | None = None
    # Confirmed country context; null means comparisons cannot infer a country.
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    operating_model: OperatingModeAssessment
    operating_location: OperatingLocation
    selected_branch_ref: str | None
    identity_match: IdentityMatch

    @model_validator(mode="after")
    def bound_confirmation(self):
        from app.report_v22.evidence_bindings import host
        if host(self.site_url) != self.normalized_domain:
            raise ValueError("identity domain mismatch")
        confirmation = self.operating_model.confirmation_ref
        if confirmation and (confirmation.normalized_domain != self.normalized_domain
                or confirmation.selected_branch_ref != self.selected_branch_ref):
            raise ValueError("confirmation belongs to another entity or branch")
        visit = self.operating_location.confirmation_ref
        if visit and (self.operating_model.value not in {"storefront", "hybrid"}
                or visit.selected_branch_ref != self.selected_branch_ref or not confirmation
                or visit.confirmed_by != confirmation.confirmed_by
                or visit.confirmed_at != confirmation.confirmed_at or visit.source_ref != confirmation.source_ref):
            raise ValueError("address confirmation belongs to another review or branch")
        return self


def legacy_service_identity(identity: BusinessIdentityV2, target_market: TargetMarket) -> BusinessIdentity:
    """Lossless three-value projection; caller retains the original v2 identity.

    Legacy primary_location means sampling market, as in frozen service inputs.
    No mutation of the v2 operating location or substitution of a fourth mode.
    """
    if identity.operating_model.value not in {"storefront", "service_area", "hybrid"}:
        raise ValueError("FIRST_SERVICE_OPERATING_MODEL_UNSUPPORTED")
    return BusinessIdentity(business_name=identity.business_name, site_url=identity.site_url,
        normalized_domain=identity.normalized_domain, public_gbp_url=identity.public_gbp_url,
        operating_model=identity.operating_model.value, primary_location=target_market)


class AlignmentIdentityV2:
    """Internal view only; never serialized in a generation request or manifest."""
    def __init__(self, identity: BusinessIdentityV2, target_market: TargetMarket):
        self.identity = identity
        self.business_name = identity.business_name
        self.site_url = identity.site_url
        self.normalized_domain = identity.normalized_domain
        self.public_gbp_url = identity.public_gbp_url
        self.operating_model = identity.operating_model.value
        self.target_market = target_market
        self.primary_location = target_market.model_copy(update={"country_code": identity.country_code or "ZZ"})
        self.address_location = identity.operating_location.location

    def model_dump(self, **kwargs):
        return self.identity.model_dump(**kwargs)
