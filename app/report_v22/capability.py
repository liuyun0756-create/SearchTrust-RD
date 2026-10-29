"""Versioned P1 capability contracts; existing 2.2.1 jobs keep their contract."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import ConfigDict, Field, FiniteFloat, ValidationInfo, field_validator, model_serializer, model_validator

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.models import StrictModel as LegacyStrictModel


class StrictModel(LegacyStrictModel):
    # Preserve exact evidence and timestamp bytes across Python and JavaScript.
    model_config = ConfigDict(str_strip_whitespace=False, revalidate_instances="always")

    @field_validator("*", mode="before")
    @classmethod
    def canonical_uuid_text(cls, value, info: ValidationInfo):
        annotation=cls.model_fields[info.field_name].annotation
        if (annotation is UUID or UUID in get_args(annotation)) and isinstance(value,str) and value != value.lower():
            raise ValueError("UUID text must be lowercase")
        return value


Text = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
Label = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
Timestamp = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$", json_schema_extra={"format":"date-time"})]
SafeInteger = Annotated[int, Field(ge=-9007199254740991, le=9007199254740991)]
Checksum = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
SourceKind = Literal["site", "serp", "competitor", "public_gbp", "gsc", "ga4", "official_gbp"]
Mode = Literal["acquisition", "after_acquisition", "first_service", "review"]
SOURCE_ORDER = ("site", "serp", "competitor", "public_gbp", "gsc", "ga4", "official_gbp")
SOURCE_SCHEMAS = dict(zip(SOURCE_ORDER, ("site_inventory_snapshot_v1", "serp_market_snapshot_v1",
    "competitor_collection_snapshot_v1", "customer_public_gbp_snapshot_v1", "gsc_sync_v1", "ga4_sync_v1", "gbp_sync_v1")))


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ExecutionConditions(StrictModel):
    capacity: Literal["unknown", "limited", "available"]
    publishing_access: Literal["unknown", "needs_approval", "available"]


class BusinessScope(StrictModel):
    id: UUID
    user_id: UUID
    case_id: UUID
    revision: int = Field(ge=1)
    site_url: str = Field(pattern=r"^https?://[^\s]+$", max_length=2000)
    primary_service: Label
    target_market: Label
    target_pages: list[Text] = Field(max_length=20)
    competitor_sites: list[Text] = Field(min_length=1, max_length=3)
    priority: Label | None
    confirmation_source: Literal["seo_confirmation", "client_confirmation"]
    confirmed_at: Timestamp
    execution_conditions: ExecutionConditions | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_scope(self, handler):
        result = handler(self)
        if self.execution_conditions is None:
            result.pop("execution_conditions", None)
        return result

    @model_validator(mode="after")
    def validate_competitors(self):
        if self.execution_conditions is None and "execution_conditions" in self.model_fields_set:
            raise ValueError("absent historical conditions must not be null")
        from urllib.parse import urlsplit
        domains = []
        for site in [self.site_url, *self.competitor_sites]:
            parsed = urlsplit(site)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("invalid business website")
            domains.append(parsed.hostname.lower().removeprefix("www."))
        if len(set(domains)) != len(domains):
            raise ValueError("duplicate business or competitor domain")
        instant(self.confirmed_at)
        return self


class SnapshotReference(StrictModel):
    snapshot_id: UUID
    case_id: UUID
    source_kind: SourceKind
    schema_version: Label
    payload_checksum: Checksum
    fetched_at: Timestamp
    expires_at: Timestamp
    health: Literal["healthy"]
    identity_match: Literal["matched"]
    binding_id: UUID | None
    scope: Text


class ReportReference(StrictModel):
    report_id: UUID
    case_id: UUID
    report_type: Literal["prospect", "verified_execution"]
    version: int = Field(ge=1)
    payload_checksum: Checksum


class ImplementationReference(StrictModel):
    id: UUID
    revision: int = Field(ge=1)
    checksum: Checksum


class AnalysisInputManifest(StrictModel):
    schema_version: Literal["v22_analysis_input_v1"]
    id: UUID
    job_id: UUID
    user_id: UUID
    case_id: UUID
    mode: Mode
    business: BusinessScope
    snapshots: list[SnapshotReference] = Field(min_length=4, max_length=7)
    evaluated_at: Timestamp
    ruleset_version: Label
    generation_input_checksum: Checksum
    previous_report: ReportReference | None
    implementation: ImplementationReference | None
    input_checksum: Checksum

    @model_validator(mode="after")
    def validate_binding(self):
        if self.business.user_id != self.user_id or self.business.case_id != self.case_id:
            raise ValueError("business ownership mismatch")
        if instant(self.business.confirmed_at) > instant(self.evaluated_at):
            raise ValueError("business confirmation is in the future")
        kinds = [s.source_kind for s in self.snapshots]
        ids = [s.snapshot_id for s in self.snapshots]
        if len(set(kinds)) != len(kinds) or len(set(ids)) != len(ids):
            raise ValueError("duplicate snapshot source or identity")
        if kinds != sorted(kinds, key=SOURCE_ORDER.index):
            raise ValueError("snapshot order is not canonical")
        required = set(SOURCE_ORDER[:4 if self.mode == "acquisition" else 6])
        if not required <= set(kinds) or (self.mode == "acquisition" and set(kinds) != required):
            raise ValueError("required sources missing or private acquisition source")
        for source in self.snapshots:
            if source.schema_version != SOURCE_SCHEMAS[source.source_kind]:
                raise ValueError("source schema mismatch")
            if source.case_id != self.case_id or not instant(source.fetched_at) <= instant(self.evaluated_at) < instant(source.expires_at):
                raise ValueError("source Case or freshness mismatch")
            if (source.source_kind in {"gsc", "ga4", "official_gbp"}) != (source.binding_id is not None):
                raise ValueError("source binding mismatch")
        if self.mode in {"acquisition", "first_service"}:
            if self.previous_report is not None:
                raise ValueError("initial analysis cannot have historical report")
        elif self.previous_report is None or self.previous_report.case_id != self.case_id:
            raise ValueError("historical report missing or wrong Case")
        elif self.previous_report.report_type != ("prospect" if self.mode == "after_acquisition" else "verified_execution"):
            raise ValueError("historical report type mismatch")
        if self.previous_report is not None and self.previous_report.report_id == self.job_id:
            raise ValueError("historical report cannot be the current task")
        if (self.mode == "review") != (self.implementation is not None):
            raise ValueError("implementation reference mismatch")
        if verified_request_digest(self.model_dump(mode="json", exclude={"input_checksum"})) != self.input_checksum:
            raise ValueError("manifest checksum mismatch")
        return self


class CapabilityEvidence(StrictModel):
    id: Label
    snapshot_id: UUID
    source_kind: SourceKind
    object: Text
    field_path: Text
    observed_at: Timestamp
    value: Annotated[str, Field(max_length=2000)] | SafeInteger | FiniteFloat | bool | None
    unit: Label | None
    comparison_scope: Text
    limitations: list[Text] = Field(max_length=20)


class CapabilityFinding(StrictModel):
    id: Label
    rule_id: Label
    rule_version: Label
    classification: Literal["fact", "inference", "estimate"]
    severity: Literal["low", "medium", "high", "critical"]
    confidence: Literal["low", "medium", "high"]
    target: Text
    observed_fact: Text
    observed_at: Timestamp
    comparison_scope: Text
    judgment: Text
    interpretation: Literal["supported", "needs_verification", "conflicting"]
    impact_path: Text
    evidence_ids: list[Label] = Field(min_length=1, max_length=20)
    limitations: list[Text] = Field(max_length=20)


class ExecutionMaterial(StrictModel):
    id: Label
    kind: Literal["title_brief", "service_brief", "profile_correction", "technical_check", "measurement_check"]
    title: Label
    target: Text
    body: Text
    readiness: Literal["instructions", "requires_confirmation", "ready_to_use"]
    confirmation_items: list[Text] = Field(max_length=12)
    evidence_ids: list[Label] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_readiness(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("duplicate material evidence")
        if self.readiness == "ready_to_use" and self.confirmation_items:
            raise ValueError("unconfirmed material cannot be ready to use")
        if self.readiness == "requires_confirmation" and not self.confirmation_items:
            raise ValueError("conditional material must name confirmation items")
        return self


class CapabilityAction(StrictModel):
    id: Label
    title: Label
    target: Text
    finding_ids: list[Label] = Field(min_length=1, max_length=20)
    steps: list[Text] = Field(min_length=1, max_length=12)
    completion: list[Text] = Field(min_length=1, max_length=12)
    review: Text
    dependencies: list[Label] = Field(max_length=2)
    materials: list[ExecutionMaterial] = Field(max_length=3)


class CapabilityChange(StrictModel):
    finding_id: Label
    previous_fingerprint: Checksum
    current_fingerprint: Checksum
    reason: Text


class PriorityCandidateInput(StrictModel):
    action_id: Label
    target: Text
    finding_ids: list[Label] = Field(min_length=1, max_length=20)
    dependencies: list[Label] = Field(max_length=2)
    material_kinds: list[Literal["title_brief", "service_brief", "profile_correction", "technical_check", "measurement_check"]] = Field(max_length=5)
    readiness: list[Literal["instructions", "requires_confirmation", "ready_to_use"]] = Field(max_length=3)


class PriorityDecision(StrictModel):
    input: PriorityCandidateInput
    prerequisite: Literal["access_indexing", "measurement", "none"]
    business_match: Literal["confirmed_target", "confirmed_objective", "not_matched", "unknown"]
    evidence_support: Literal["supported", "needs_verification", "conflicting"]
    execution: Literal["confirmation_required", "instructions_only", "ready_to_use"]
    severity: Literal["low", "medium", "high", "critical"]
    confidence: Literal["low", "medium", "high"]
    impact_paths: list[Text] = Field(min_length=1, max_length=20)
    comparison_scopes: list[Text] = Field(min_length=1, max_length=20)
    reason_codes: list[Label] = Field(min_length=1, max_length=12)
    explanation: Text
    selected_order: int | None = Field(ge=1, le=3)


class PrioritizationTrace(StrictModel):
    rules_version: Literal["v22_business_priority_v1", "v22_business_priority_v2"]
    input_checksum: Checksum
    business_priority: Label | None
    target_pages: list[Text] = Field(max_length=20)
    action_order: list[Label] = Field(max_length=3)
    candidates: list[PriorityDecision] = Field(max_length=1000)
    limitations: list[Text] = Field(min_length=1, max_length=5)


class CapabilityReport(StrictModel):
    schema_version: Literal["2.2.2"]
    id: UUID
    report_type: Literal["prospect", "verified_execution"]
    version: int = Field(ge=1)
    generated_at: Timestamp
    input_manifest: AnalysisInputManifest
    title: Label
    summary: Text
    evidence: list[CapabilityEvidence] = Field(min_length=1, max_length=500)
    findings: list[CapabilityFinding] = Field(min_length=1, max_length=50)
    action_state: Literal["actions_supported", "insufficient_basis"]
    actions: list[CapabilityAction] = Field(max_length=3)
    limitations: list[Text] = Field(min_length=1, max_length=40)
    changes: list[CapabilityChange] = Field(max_length=50)
    settlement_policy: Literal["one_credit_per_completed_analysis"]
    prioritization: PrioritizationTrace | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_bytes(self, handler):
        result = handler(self)
        # Adding optional metadata must not change historical report digests.
        if self.prioritization is None:
            result.pop("prioritization", None)
        return result

    @model_validator(mode="after")
    def validate_references(self):
        manifest = self.input_manifest
        if self.id != manifest.job_id or instant(self.generated_at) < instant(manifest.evaluated_at):
            raise ValueError("report task or evaluation binding mismatch")
        if self.report_type != ("prospect" if manifest.mode == "acquisition" else "verified_execution"):
            raise ValueError("report type mismatch")
        if self.version != (1 if manifest.previous_report is None else manifest.previous_report.version + 1):
            raise ValueError("report version mismatch")
        if bool(self.actions) != (self.action_state == "actions_supported"):
            raise ValueError("action result state mismatch")
        for items in (self.evidence, self.findings, self.actions):
            if len({item.id for item in items}) != len(items):
                raise ValueError("duplicate result identity")
        snapshots = {s.snapshot_id: s for s in manifest.snapshots}
        evidence = {e.id: e for e in self.evidence}
        findings = {f.id: f for f in self.findings}
        prior: set[str] = set()
        for item in self.evidence:
            snapshot = snapshots.get(item.snapshot_id)
            if snapshot is None or snapshot.source_kind != item.source_kind or item.observed_at != snapshot.fetched_at:
                raise ValueError("evidence source binding mismatch")
        for finding in self.findings:
            if len(set(finding.evidence_ids)) != len(finding.evidence_ids) or not set(finding.evidence_ids) <= evidence.keys():
                raise ValueError("finding references unknown or duplicate evidence")
            if instant(finding.observed_at) != max(instant(evidence[key].observed_at) for key in finding.evidence_ids):
                raise ValueError("finding observation time mismatch")
        for action in self.actions:
            if not set(action.finding_ids) <= findings.keys() or len(set(action.finding_ids)) != len(action.finding_ids):
                raise ValueError("action references unknown or duplicate findings")
            if not set(action.dependencies) <= prior or len(set(action.dependencies)) != len(action.dependencies):
                raise ValueError("action dependencies must precede action")
            allowed_evidence = {key for fid in action.finding_ids for key in findings[fid].evidence_ids}
            if len({item.id for item in action.materials}) != len(action.materials):
                raise ValueError("duplicate material identity")
            if any(not set(item.evidence_ids) <= allowed_evidence for item in action.materials):
                raise ValueError("material references evidence outside its action")
            prior.add(action.id)
        for change in self.changes:
            if manifest.previous_report is None or change.finding_id not in findings or change.previous_fingerprint == change.current_fingerprint:
                raise ValueError("invalid or unchanged history badge")
            if change.current_fingerprint != verified_request_digest(findings[change.finding_id].model_dump(mode="json")):
                raise ValueError("changed finding fingerprint mismatch")
        if len({c.finding_id for c in self.changes}) != len(self.changes):
            raise ValueError("duplicate history badge")
        priority_rules = {"v22_capability_public_v2", "v22_capability_service_v2", "v22_capability_public_v3", "v22_capability_service_v3", "v22_capability_public_v4", "v22_capability_service_v4"}
        if self.prioritization is None and "prioritization" in self.model_fields_set:
            raise ValueError("absent historical priority must not be serialized as null")
        if (manifest.ruleset_version in priority_rules) != (self.prioritization is not None):
            raise ValueError("priority trace ruleset mismatch")
        if self.prioritization is not None:
            from app.report_v22.capability_priority import candidate_input, prioritize
            trace = self.prioritization.model_dump(mode="json")
            expected_version = "v22_business_priority_v2" if manifest.ruleset_version.endswith(("_v3", "_v4")) else "v22_business_priority_v1"
            if trace["rules_version"] != expected_version:
                raise ValueError("priority version mismatch")
            replay = prioritize([entry["input"] for entry in trace["candidates"]],
                [f.model_dump(mode="json") for f in self.findings], manifest.business.model_dump(mode="json"), manifest.input_checksum,
                rules_version=expected_version)
            if trace != replay or trace["action_order"] != [a.id for a in self.actions]:
                raise ValueError("priority trace does not reproduce report order")
            inputs = {e["input"]["action_id"]: e["input"] for e in trace["candidates"]}
            if any(inputs.get(a.id) != candidate_input(a.model_dump(mode="json")) for a in self.actions):
                raise ValueError("priority inputs do not match delivered action")
        return self

    def validate_previous_report(self, previous: "CapabilityReport"):
        reference = self.input_manifest.previous_report
        if (reference is None or reference.report_id != previous.id
                or reference.case_id != previous.input_manifest.case_id
                or self.input_manifest.user_id != previous.input_manifest.user_id
                or reference.report_type != previous.report_type or reference.version != previous.version
                or reference.payload_checksum != verified_request_digest(previous.model_dump(mode="json"))):
            raise ValueError("historical report binding mismatch")
        prior = {f.id: verified_request_digest(f.model_dump(mode="json")) for f in previous.findings}
        if any(prior.get(c.finding_id) != c.previous_fingerprint for c in self.changes):
            raise ValueError("historical finding fingerprint mismatch")
        return self


class EditableAction(StrictModel):
    id: Label
    title: Label
    target: Text
    steps: list[Text] = Field(min_length=1, max_length=12)
    completion: list[Text] = Field(min_length=1, max_length=12)
    review: Text
    materials: list[ExecutionMaterial] = Field(max_length=3)


class DeliveryRevision(StrictModel):
    schema_version: Literal["v22_delivery_revision_v1"]
    report_id: UUID
    user_id: UUID
    case_id: UUID
    expected_revision: int = Field(ge=0)
    source_checksum: Checksum
    status: Literal["draft", "approved"]
    title: Label
    summary: Text
    client_note: Text | None
    actions: list[EditableAction] = Field(max_length=3)

    def validate_report(self, report: CapabilityReport):
        report = CapabilityReport.model_validate(report)
        manifest = report.input_manifest
        if (self.report_id != report.id or self.user_id != manifest.user_id or self.case_id != manifest.case_id
                or self.source_checksum != verified_request_digest(report.model_dump(mode="json"))):
            raise ValueError("delivery source binding mismatch")
        ids = [a.id for a in self.actions]
        if len(set(ids)) != len(ids) or set(ids) != {a.id for a in report.actions}:
            raise ValueError("delivery must preserve system action identities")
        originals = {a.id: a for a in report.actions}
        if report.prioritization and report.prioritization.rules_version == "v22_business_priority_v2":
            from app.report_v22.capability_priority import valid_delivery_order
            if not valid_delivery_order(ids, report.prioritization.model_dump(mode="json")):
                raise ValueError("delivery order violates prerequisites")
        for action in self.actions:
            source = {item.id: item for item in originals[action.id].materials}
            if len({item.id for item in action.materials}) != len(action.materials) or {item.id for item in action.materials} != source.keys():
                raise ValueError("delivery must preserve material identities")
            for item in action.materials:
                if item.model_dump(exclude={"title", "body"}) != source[item.id].model_dump(exclude={"title", "body"}):
                    raise ValueError("material evidence and confirmation state are immutable")
        return self


class CapabilityContracts(StrictModel):
    report: CapabilityReport
    delivery_revision: DeliveryRevision
