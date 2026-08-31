"""Internal-only candidate contracts; no confirmed primary business values."""
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, ConfigDict, Field, HttpUrl, WrapValidator, model_validator

from app.collectors.site_inventory_models import InventoryErrorCode, PageCheckStatus, SitePageType
from app.report_v22.evidence_models import EvidenceSourceTrace, SiteEvidenceSource
from app.report_v22.models import EvidenceId, EvidenceItem, StrictModel

VERSION = "site_business_extraction_v1"
FIELD_NAMES = ("business_name", "address", "phone", "service_area")
COMPONENTS = ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
FieldKind = Literal["business_name", "address", "phone", "service_area"]
Component = Literal["streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"]
SourceKind = Literal["jsonld_property", "tel_link", "dom_address", "dom_label"]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
CandidateId = Annotated[str, Field(pattern=r"^sf_[a-f0-9]{64}$")]
DiagnosticCode = Literal[
    "no_snapshot", "source_expired", "source_unavailable", "source_identity_mismatch", "page_not_checked",
    "deep_snapshot_missing", "http_error", "content_unsupported", "out_of_scope_redirect", "html_missing",
    "html_parse_failed", "jsonld_parse_failed", "duplicate_json_key", "unsupported_context",
    "unsupported_field_shape", "invalid_field_value", "external_entity_hint", "ownership_unresolved",
]


def bounded_url(value):
    if len(str(value)) > 2083:
        raise ValueError("URL exceeds limit")
    return value


SiteUrl = Annotated[HttpUrl, AfterValidator(bounded_url)]


class CandidateModel(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)


class SiteBusinessLimits(CandidateModel):
    max_input_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)
    max_output_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)
    max_inventory_pages: int = Field(default=500, ge=1, le=500)
    max_deep_pages: int = Field(default=50, ge=1, le=50)
    max_html_bytes_per_page: int = Field(default=1_000_000, ge=1, le=1_000_000)
    max_html_bytes_total: int = Field(default=10_000_000, ge=1, le=10_000_000)
    max_html_depth: int = Field(default=64, ge=1, le=64)
    max_html_nodes_per_page: int = Field(default=20_000, ge=1, le=20_000)
    max_html_nodes_total: int = Field(default=200_000, ge=1, le=200_000)
    max_jsonld_blocks: int = Field(default=32, ge=1, le=32)
    max_jsonld_bytes: int = Field(default=65_536, ge=1, le=65_536)
    max_json_depth: int = Field(default=32, ge=1, le=32)
    max_json_nodes_per_page: int = Field(default=10_000, ge=1, le=10_000)
    max_field_values: int = Field(default=50, ge=1, le=50)
    max_candidates_per_page: int = Field(default=250, ge=1, le=250)
    max_candidates: int = Field(default=5_000, ge=1, le=5_000)
    max_evidence: int = Field(default=20_000, ge=1, le=20_000)
    max_origins: int = Field(default=20_000, ge=1, le=20_000)


class SiteBusinessFactsContext(CandidateModel):
    case_id: UUID
    report_type: Literal["prospect"]
    site_url: SiteUrl
    evaluated_at: AwareDatetime


def preserve_saved_html(value, handler, info):
    """Reuse the frozen source contract without its legacy HTML whitespace cleanup.

    Only restore an already type/length-checked HTML string, never other unvalidated
    fields. Copies keep caller-owned snapshots immutable.
    """
    # Pydantic 2.10's wrap handler treats strict nested datetimes as Python
    # inputs. Explicit JSON validation preserves the frozen JSON contract.
    validated = (SiteEvidenceSource.model_validate_json(json.dumps(value, allow_nan=False))
                 if info.mode == "json" and value is not None else handler(value))
    if validated is None:
        return None

    def get(item, key):
        return item.get(key) if isinstance(item, dict) else getattr(item, key)

    selected = []
    for original, checked in zip(get(get(value, "payload"), "selected_pages") or [], validated.payload.selected_pages, strict=True):
        deep = get(original, "deep_snapshot")
        if deep is not None:
            html = get(deep, "html")
            if type(html) is not str or len(html) > 5_000_000:
                raise ValueError("Invalid saved HTML")
            checked = checked.model_copy(update={"deep_snapshot": checked.deep_snapshot.model_copy(update={"html": html})})
        selected.append(checked)
    return validated.model_copy(update={"payload": validated.payload.model_copy(update={"selected_pages": selected})})


class SiteBusinessFactsInput(CandidateModel):
    context: SiteBusinessFactsContext
    source: Annotated[SiteEvidenceSource | None, WrapValidator(preserve_saved_html)]
    limits: SiteBusinessLimits = Field(default_factory=SiteBusinessLimits)


class CandidateOrigin(CandidateModel):
    origin_path: str
    decoded_html_checksum: Digest
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    kind: Literal["jsonld", "dom"]
    json_pointer: str | None = None
    record_pointer: str | None = None
    element_path: list[int] = Field(default_factory=list, max_length=64)
    attribute: Literal["href"] | None = None
    component: Component | None = None
    excerpt: str = Field(max_length=360)
    excerpt_truncated: bool

    @model_validator(mode="after")
    def layout(self):
        if self.end <= self.start or any(i < 0 for i in self.element_path):
            raise ValueError("invalid origin span")
        if self.kind == "jsonld":
            if self.json_pointer is None or self.record_pointer is None or self.element_path or self.attribute:
                raise ValueError("invalid JSON origin")
        elif self.json_pointer is not None or self.record_pointer is not None or not self.element_path:
            raise ValueError("invalid DOM origin")
        return self


class CandidateDiagnostic(CandidateModel):
    code: DiagnosticCode
    fields: list[FieldKind] = Field(default_factory=list)
    scope: Literal["extraction", "ownership", "availability"] = "extraction"
    origin_path: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    json_pointer: str | None = None

    @model_validator(mode="after")
    def canonical(self):
        self.fields = sorted(set(self.fields))
        return self


class CandidateDraft(CandidateModel):
    field: FieldKind
    source_kind: SourceKind
    entity_key: Digest | None = None
    ownership_status: Literal["declared_entity", "unresolved"]
    declared_id: str | None = Field(default=None, max_length=2083)
    declared_url: str | None = Field(default=None, max_length=2083)
    scalar_value: str | None
    components: dict[Component, str] = Field(default_factory=dict)
    origins: list[CandidateOrigin] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def shape(self):
        if self.field == "address" and self.scalar_value is None:
            if not self.components:
                raise ValueError("address requires actual components")
        elif self.scalar_value is None or self.components:
            raise ValueError("invalid candidate scalar")
        bound = {"business_name": 240, "phone": 120, "service_area": 240, "address": 500}[self.field]
        if self.scalar_value is not None and (not self.scalar_value.strip() or len(self.scalar_value) > bound):
            raise ValueError("invalid candidate value")
        if self.field == "phone" and not any(c.isdigit() for c in self.scalar_value):
            raise ValueError("phone has no digits")
        if any(not value.strip() or len(value) > (500 if key == "streetAddress" else 240) for key, value in self.components.items()):
            raise ValueError("invalid address component")
        if self.source_kind != "jsonld_property" and (self.entity_key is not None or self.ownership_status != "unresolved" or self.declared_id is not None or self.declared_url is not None):
            raise ValueError("DOM does not establish entity ownership")
        if self.source_kind == "jsonld_property" and self.entity_key is None:
            raise ValueError("structured record requires entity context")
        return self


class SiteBusinessCandidate(CandidateDraft):
    candidate_id: CandidateId
    snapshot_id: UUID
    requested_url: SiteUrl
    final_url: SiteUrl
    collected_at: AwareDatetime
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class CandidateFieldStatus(CandidateModel):
    observation_status: Literal["observed", "not_observed", "not_checked"]
    ownership_status: Literal["declared_entity", "unresolved", "mixed", "not_applicable"]
    candidate_ids: list[CandidateId] = Field(default_factory=list)


class CandidatePage(CandidateModel):
    requested_url: SiteUrl
    page_type: SitePageType
    inventory_check_status: PageCheckStatus
    inventory_error_code: InventoryErrorCode | None
    status: Literal["parsed", "partial", "not_checked", "parse_failed"]
    final_url: SiteUrl | None = None
    collected_at: AwareDatetime | None = None
    response_status: int | None = None
    content_type: str | None = None
    content_checksum: Digest | None = None
    decoded_html_checksum: Digest | None = None
    fields: dict[FieldKind, CandidateFieldStatus]
    diagnostics: list[CandidateDiagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def all_fields(self):
        if set(self.fields) != set(FIELD_NAMES):
            raise ValueError("all candidate fields required")
        return self


class CandidateSourceStatus(CandidateModel):
    state: Literal["ready", "missing", "ineligible"]
    reason: Literal["no_snapshot", "source_expired", "source_unavailable", "source_identity_mismatch"] | None

    @model_validator(mode="after")
    def state_reason(self):
        if (self.state == "ready" and self.reason is not None or self.state == "missing" and self.reason != "no_snapshot"
                or self.state == "ineligible" and self.reason not in {"source_expired", "source_unavailable", "source_identity_mismatch"}):
            raise ValueError("invalid source state")
        return self


class SiteBusinessFactsResult(CandidateModel):
    schema_version: Literal["site_business_facts_v1"] = "site_business_facts_v1"
    extraction_version: Literal["site_business_extraction_v1"] = VERSION
    case_id: UUID
    site_url: SiteUrl
    evaluated_at: AwareDatetime
    source_snapshot_id: UUID | None
    source_payload_checksum: Digest | None
    source_status: CandidateSourceStatus
    pages: list[CandidatePage]
    candidates: list[SiteBusinessCandidate]
    evidence_index: list[EvidenceItem]
    source_traces: list[EvidenceSourceTrace]
    limitations: list[str]
