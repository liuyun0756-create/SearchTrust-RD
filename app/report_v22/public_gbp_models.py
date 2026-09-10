"""Internal normalized observations; not an HTTP API or a provider decoder."""
from datetime import timedelta
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.security_v22.urls import validate_gbp_url
from app.report_v22.models import StrictModel

IDENTITY_VERSION = "customer_public_gbp_identity_v1"
KEY_ORDER = ("place_id", "data_id", "cid")
FIELD_NAMES = ("business_name", "website_url", "address", "phone", "service_areas", "service_area_business")
FieldState = Literal["observed", "not_returned", "returned_empty", "unsupported", "request_failed"]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
Name = Annotated[str, Field(min_length=1, max_length=240)]
Address = Annotated[str, Field(min_length=1, max_length=500)]
Phone = Annotated[str, Field(min_length=1, max_length=120)]


def bounded_url(value: HttpUrl) -> HttpUrl:
    if len(str(value)) > 2083:
        raise ValueError("URL exceeds limit")
    return value


WebsiteUrl = Annotated[HttpUrl, AfterValidator(bounded_url)]


def google_url(value: HttpUrl) -> HttpUrl:
    validate_gbp_url(str(value))
    return value


GoogleUrl = Annotated[WebsiteUrl, AfterValidator(google_url)]


class PublicGbpModel(StrictModel):
    # Opaque keys and observed text must not be silently normalized.
    model_config = ConfigDict(str_strip_whitespace=False)


class PublicGbpEntityKey(PublicGbpModel):
    kind: Literal["place_id", "data_id", "cid"]
    value: str = Field(min_length=1, max_length=480)

    @field_validator("value")
    @classmethod
    def no_padding(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError("invalid entity key")
        return value


def ordered_keys(values):
    if len({item.kind for item in values}) != len(values):
        raise ValueError("duplicate entity key type")
    return sorted(values, key=lambda item: KEY_ORDER.index(item.kind))


class PublicGbpRequestTarget(PublicGbpModel):
    public_gbp_url: GoogleUrl
    entity_keys: list[PublicGbpEntityKey] = Field(max_length=3)

    _ordered_keys = field_validator("entity_keys")(ordered_keys)


class CustomerPublicGbpReference(PublicGbpRequestTarget):
    case_id: UUID
    site_url: WebsiteUrl
    confirmation_source: Literal["user"]
    confirmed_at: AwareDatetime


T = TypeVar("T")


class PublicGbpField(PublicGbpModel, Generic[T]):
    state: FieldState
    value: T

    @model_validator(mode="after")
    def check_presence(self):
        value = self.value
        if self.state == "observed":
            if value is None or (isinstance(value, list) and not value):
                raise ValueError("observed field needs a value")
            text_values = value if isinstance(value, list) else [value]
            if any(isinstance(item, str) and not item.strip() for item in text_values):
                raise ValueError("observed text must not be blank")
        elif value is not None and value != []:
            raise ValueError("unobserved field cannot contain a value")
        return self


class PublicGbpFields(PublicGbpModel):
    business_name: PublicGbpField[Name | None]
    website_url: PublicGbpField[WebsiteUrl | None]
    address: PublicGbpField[Address | None]
    phone: PublicGbpField[Phone | None]
    service_areas: PublicGbpField[Annotated[list[Name], Field(max_length=50)]]
    service_area_business: PublicGbpField[bool | None]


class PublicGbpRecord(PublicGbpModel):
    observed_entity_keys: list[PublicGbpEntityKey] = Field(max_length=3)
    observed_public_gbp_url: GoogleUrl | None
    fields: PublicGbpFields

    _ordered_keys = field_validator("observed_entity_keys")(ordered_keys)


class PublicGbpLimits(PublicGbpModel):
    max_bytes: int = Field(default=1_000_000, ge=1, le=1_000_000)


class PublicGbpSample(PublicGbpModel):
    request_target: PublicGbpRequestTarget
    started_at: AwareDatetime
    completed_at: AwareDatetime
    expires_at: AwareDatetime
    provider: Literal["serpapi_public"]
    request_record_id: str = Field(pattern=r"^req_[a-z0-9]{16,80}$")
    response_checksum: Digest | None
    collection_status: Literal["succeeded", "failed"]
    failure_code: Literal["timeout", "provider_unavailable", "request_rejected", "invalid_response"] | None
    record: PublicGbpRecord
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_sample(self):
        if not self.started_at <= self.completed_at < self.expires_at <= self.completed_at + timedelta(days=30):
            raise ValueError("invalid public profile interval")
        states = [getattr(self.record.fields, field).state for field in FIELD_NAMES]
        if self.collection_status == "succeeded":
            if self.response_checksum is None or self.failure_code is not None or "request_failed" in states:
                raise ValueError("inconsistent successful sample")
        elif (self.failure_code is None or self.record.observed_entity_keys or self.record.observed_public_gbp_url is not None
              or any(state != "request_failed" for state in states)):
            raise ValueError("inconsistent failed sample")
        return self


class CustomerPublicGbpSnapshotInput(PublicGbpSample):
    reference: CustomerPublicGbpReference
    limits: PublicGbpLimits = Field(default_factory=PublicGbpLimits)

    @model_validator(mode="after")
    def confirmation_precedes_sample(self):
        if self.reference.confirmed_at > self.started_at:
            raise ValueError("confirmation must precede sampling")
        return self


class CustomerPublicGbpSnapshot(PublicGbpSample):
    schema_version: Literal["customer_public_gbp_snapshot_v1"] = "customer_public_gbp_snapshot_v1"
    subject_reference_checksum: Digest
    identity_rule_version: Literal["customer_public_gbp_identity_v1"] = IDENTITY_VERSION
    identity_match_status: Literal["matched", "mismatch", "needs_confirmation"]
    identity_reasons: list[Literal["lookup_failed", "strong_id_conflict", "no_comparable_strong_id",
                                 "website_domain_conflict", "strong_id_matched", "website_not_observed"]]
    health_status: Literal["healthy", "unavailable"]
