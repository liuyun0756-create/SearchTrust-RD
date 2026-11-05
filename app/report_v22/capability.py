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
SourceKind = Literal["site", "serp", "competitor", "public_gbp", "gsc", "ga4", "official_gbp", "rendered"]
Mode = Literal["acquisition", "after_acquisition", "first_service", "review"]
SOURCE_ORDER = ("site", "serp", "competitor", "public_gbp", "gsc", "ga4", "official_gbp", "rendered")
SOURCE_SCHEMAS = dict(zip(SOURCE_ORDER, ("site_inventory_snapshot_v1", "serp_market_snapshot_v1",
    "competitor_collection_snapshot_v1", "customer_public_gbp_snapshot_v1", "gsc_sync_v1", "ga4_sync_v1", "gbp_sync_v1", "site_rendered_snapshot_v1")))


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
    schema_version: Literal["v22_analysis_input_v1", "v22_analysis_input_v2"]
    id: UUID
    job_id: UUID
    user_id: UUID
    case_id: UUID
    mode: Mode
    business: BusinessScope
    snapshots: list[SnapshotReference] = Field(min_length=4, max_length=8)
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
        if self.schema_version == "v22_analysis_input_v2":
            if self.mode != "acquisition" or self.ruleset_version not in {"v22_capability_public_v7", "v22_capability_public_v8", "v22_capability_public_v9", "v22_capability_public_v10", "v22_capability_public_v11", "v22_capability_public_v12"}:
                raise ValueError("rendered inputs require their own acquisition ruleset")
            required.add("rendered")
        elif "rendered" in kinds or self.ruleset_version == "v22_capability_public_v7":
            raise ValueError("rendered source requires a versioned manifest")
        if not required <= set(kinds) or (self.mode == "acquisition" and set(kinds) != required):
            raise ValueError("required sources missing or private acquisition source")
        for source in self.snapshots:
            if source.schema_version != SOURCE_SCHEMAS[source.source_kind] and not (
                    (source.source_kind == "public_gbp" and source.schema_version == "customer_public_gbp_snapshot_v2")
                    or (source.source_kind == "site" and source.schema_version == "site_inventory_snapshot_v3" and self.ruleset_version in {"v22_capability_public_v11", "v22_capability_public_v12"})
                    or (source.source_kind == "site" and source.schema_version == "site_inventory_snapshot_v2" and self.ruleset_version in {"v22_capability_public_v9", "v22_capability_public_v10", "v22_capability_public_v11", "v22_capability_public_v12"})):
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


class FindingCoverage(StrictModel):
    source: Literal["site", "gbp", "serp", "competitor", "citation", "gsc", "ga4", "rendered"]
    checked: bool


class CapabilityFinding(StrictModel):
    id: Label
    rule_id: Label
    rule_version: Label
    classification: Literal["fact", "inference", "estimate"]
    kind: Literal["fact", "inference", "recommendation"] | None = None
    status: Literal["confirmed", "not_detected", "not_checked", "not_assessed"] | None = None
    coverage: list[FindingCoverage] | None = Field(default=None, min_length=1, max_length=8)

    @model_serializer(mode="wrap")
    def preserve_unlabelled_findings(self, handler):
        value = handler(self)
        for field in ("kind", "status", "coverage"):
            if getattr(self, field) is None:
                value.pop(field, None)
        return value

    @model_validator(mode="after")
    def validate_labels(self):
        labels = (self.kind, self.status, self.coverage)
        if any(v is not None for v in labels):
            if any(v is None for v in labels):
                raise ValueError("finding labels must be complete")
            if self.kind != "fact" and self.status != "not_assessed":
                raise ValueError("non-factual findings cannot claim factual confirmation")
            if len({v.source for v in self.coverage}) != len(self.coverage):
                raise ValueError("duplicate coverage source")
        elif {"kind", "status", "coverage"} & self.model_fields_set:
            raise ValueError("historical finding labels must be absent, not null")
        return self
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
    technical_finding_ids: list[Label] | None = Field(default=None, max_length=20)
    default_visible: bool | None = None

    @model_serializer(mode="wrap")
    def preserve_old(self, handler):
        result = handler(self)
        for key in ("technical_finding_ids", "default_visible"):
            if getattr(self, key) is None: result.pop(key, None)
        return result

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
    candidate_order: list[Label] | None = Field(default=None, max_length=1000)

    @model_serializer(mode="wrap")
    def preserve_old(self, handler):
        result = handler(self)
        if self.candidate_order is None: result.pop("candidate_order", None)
        return result

    rules_version: Literal["v22_business_priority_v1", "v22_business_priority_v2", "v22_business_priority_v3"]
    input_checksum: Checksum
    business_priority: Label | None
    target_pages: list[Text] = Field(max_length=20)
    action_order: list[Label] = Field(max_length=3)
    candidates: list[PriorityDecision] = Field(max_length=1000)
    limitations: list[Text] = Field(min_length=1, max_length=5)


    @model_validator(mode="after")
    def versioned_candidate_metadata(self):
        v3 = self.rules_version == "v22_business_priority_v3"
        if v3:
            if self.candidate_order is None or any(e.technical_finding_ids is None or e.default_visible is None for e in self.candidates):
                raise ValueError("v3 requires complete candidate metadata")
        elif "candidate_order" in self.model_fields_set or any({"technical_finding_ids", "default_visible"} & e.model_fields_set for e in self.candidates):
            raise ValueError("new candidate metadata requires v3")
        return self


class ContactCheck(StrictModel):
    result: Literal["match", "partial_match", "mismatch", "none"]
    field: Literal["phone", "address", "business_name", "service_area", "opening_hours"]
    status: Literal["confirmed", "not_detected", "not_checked", "not_assessed"]
    basis: Literal["structured", "text_pattern", "rendered"]
    compared_components: list[Label] = Field(max_length=20)
    omitted_components: list[Label] = Field(max_length=20)
    evidence_ids: list[Label] = Field(max_length=500)

    @model_validator(mode="after")
    def unique_references(self):
        for items in (self.compared_components, self.omitted_components, self.evidence_ids):
            if len(items) != len(set(items)):
                raise ValueError("duplicate contact check component or reference")
        return self


class CapabilityReport(StrictModel):
    model_config = ConfigDict(json_schema_extra={
        "allOf": [
            {"if": {"properties": {"schema_version": {"const": version}}},
             "then": {"properties": {"input_manifest": {"type": "object", "properties": {"ruleset_version": {"const": rules}}}}}}
            for version, rules in (("2.2.6", "v22_capability_public_v11"), ("2.2.7", "v22_capability_public_v12"))
        ] + [{"if": {"properties": {"schema_version": {"const": "2.2.7"}}},
              "then": {"required": ["action_candidates"], "properties": {"action_candidates": {"type": "array"}}},
              "else": {"not": {"properties": {"action_candidates": {}}, "required": ["action_candidates"]}}},
             {"if": {"properties": {"schema_version": {"enum": ["2.2.6", "2.2.7"]}}},
              "else": {"properties": {"contact_checks": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
                "field": {"enum": ["phone", "address", "business_name", "service_area"]}}}}}}}],
        "if": {"properties": {"schema_version": {"enum": ["2.2.5", "2.2.6", "2.2.7"]}}, "required": ["schema_version"]},
        "then": {
            "if": {"properties": {"input_manifest": {"type": "object", "properties": {
                "ruleset_version": {"enum": ["v22_capability_public_v8", "v22_capability_public_v9", "v22_capability_public_v10", "v22_capability_public_v11", "v22_capability_public_v12"]}}, "required": ["ruleset_version"]}},
                "required": ["input_manifest"]},
            "then": {"properties": {"contact_checks": {"type": "array"}}, "required": ["contact_checks"]},
            "else": {"not": {"properties": {"contact_checks": {}}, "required": ["contact_checks"]}},
            "properties": {"findings": {"type": "array", "items": {
            "type": "object", "required": ["kind", "status", "coverage"],
            "properties": {"kind": {"enum": ["fact", "inference", "recommendation"]},
                "status": {"enum": ["confirmed", "not_detected", "not_checked", "not_assessed"]},
                "coverage": {"type": "array"}}
        }}}},
        "else": {"not": {"properties": {"contact_checks": {}}, "required": ["contact_checks"]},
            "properties": {"findings": {"type": "array", "items": {
            "type": "object", "not": {"anyOf": [{"properties": {name: {}}, "required": [name]} for name in ("kind", "status", "coverage")]}
        }}}}
    })
    schema_version: Literal["2.2.2", "2.2.4", "2.2.5", "2.2.6", "2.2.7"]
    id: UUID
    report_type: Literal["prospect", "verified_execution"]
    version: int = Field(ge=1)
    generated_at: Timestamp
    input_manifest: AnalysisInputManifest
    title: Label
    summary: Text
    evidence: list[CapabilityEvidence] = Field(min_length=1, max_length=500)
    findings: list[CapabilityFinding] = Field(min_length=1, max_length=50)
    contact_checks: list[ContactCheck] | None = Field(default=None, max_length=5)
    action_state: Literal["actions_supported", "insufficient_basis"]
    action_candidates: list[CapabilityAction] | None = Field(default=None, max_length=1000)
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
        if self.contact_checks is None:
            result.pop("contact_checks", None)
        if self.action_candidates is None:
            result.pop("action_candidates", None)
        return result

    @model_validator(mode="after")
    def validate_references(self):
        manifest = self.input_manifest
        v12 = manifest.ruleset_version == "v22_capability_public_v12"
        if (self.schema_version == "2.2.7") != v12 or v12 != (self.action_candidates is not None):
            raise ValueError("candidate pool requires v12")
        if not v12 and "action_candidates" in self.model_fields_set:
            raise ValueError("historical report cannot contain candidate pool")
        labelled = self.schema_version in {"2.2.5", "2.2.6", "2.2.7"}
        if (self.schema_version == "2.2.6") != (manifest.ruleset_version == "v22_capability_public_v11"):
            raise ValueError("hours require v11")
        if labelled != (manifest.ruleset_version in {"v22_capability_public_v8", "v22_capability_public_v9", "v22_capability_public_v10", "v22_capability_public_v11", "v22_capability_public_v12", "v22_capability_service_v5"}):
            raise ValueError("finding labels require a versioned ruleset")
        if any((f.kind is not None) != labelled for f in self.findings):
            raise ValueError("finding labels require the new report version")
        if not labelled and (self.schema_version == "2.2.4") != (manifest.schema_version == "v22_analysis_input_v2"):
            raise ValueError("report and manifest versions disagree")
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
        has_checks = manifest.ruleset_version in {"v22_capability_public_v8", "v22_capability_public_v9", "v22_capability_public_v10", "v22_capability_public_v11", "v22_capability_public_v12"}
        if has_checks != (self.contact_checks is not None) or (not has_checks and "contact_checks" in self.model_fields_set):
            raise ValueError("contact checks require public v8")
        checks = self.contact_checks or []
        if manifest.ruleset_version not in {"v22_capability_public_v11", "v22_capability_public_v12"} and (len(checks)>4 or any(c.field=="opening_hours" for c in checks)):
            raise ValueError("hours require v11")
        if len({c.field for c in checks}) != len(checks):
            raise ValueError("duplicate contact check field")
        if any(not set(c.evidence_ids) <= evidence.keys() for c in checks):
            raise ValueError("contact check references unknown evidence")
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
        for action in (self.action_candidates if v12 else self.actions):
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
        if v12:
            pool = {a.id: a for a in self.action_candidates}
            if len(pool) != len(self.action_candidates) or any(pool.get(a.id) != a for a in self.actions):
                raise ValueError("selected action must equal frozen candidate")
            seen = set()
            for a in self.actions:
                if not set(a.dependencies) <= seen: raise ValueError("selected dependencies missing")
                seen.add(a.id)
        for change in self.changes:
            if manifest.previous_report is None or change.finding_id not in findings or change.previous_fingerprint == change.current_fingerprint:
                raise ValueError("invalid or unchanged history badge")
            if change.current_fingerprint != verified_request_digest(findings[change.finding_id].model_dump(mode="json")):
                raise ValueError("changed finding fingerprint mismatch")
        if len({c.finding_id for c in self.changes}) != len(self.changes):
            raise ValueError("duplicate history badge")
        priority_rules = {"v22_capability_public_v2", "v22_capability_service_v2", "v22_capability_public_v3", "v22_capability_service_v3", "v22_capability_public_v4", "v22_capability_service_v4", "v22_capability_public_v5", "v22_capability_public_v6", "v22_capability_public_v7", "v22_capability_public_v8", "v22_capability_public_v9", "v22_capability_public_v10", "v22_capability_public_v11", "v22_capability_public_v12", "v22_capability_service_v5"}
        if self.prioritization is None and "prioritization" in self.model_fields_set:
            raise ValueError("absent historical priority must not be serialized as null")
        if (manifest.ruleset_version in priority_rules) != (self.prioritization is not None):
            raise ValueError("priority trace ruleset mismatch")
        if self.prioritization is not None:
            from app.report_v22.capability_priority import candidate_input, prioritize
            trace = self.prioritization.model_dump(mode="json")
            expected_version = "v22_business_priority_v3" if v12 else "v22_business_priority_v2" if manifest.ruleset_version.endswith(("_v3", "_v4", "_v5", "_v6", "_v7", "_v8", "_v9", "_v10", "_v11")) else "v22_business_priority_v1"
            if trace["rules_version"] != expected_version:
                raise ValueError("priority version mismatch")
            replay = prioritize([entry["input"] for entry in trace["candidates"]],
                [f.model_dump(mode="json") for f in self.findings], manifest.business.model_dump(mode="json"), manifest.input_checksum,
                rules_version=expected_version)
            if trace != replay or trace["action_order"] != [a.id for a in self.actions]:
                raise ValueError("priority trace does not reproduce report order")
            inputs = {e["input"]["action_id"]: e["input"] for e in trace["candidates"]}
            if any(inputs.get(a.id) != candidate_input(a.model_dump(mode="json")) for a in (self.action_candidates if v12 else self.actions)):
                raise ValueError("priority inputs do not match delivered action")
            if v12 and trace.get("candidate_order") != [a.id for a in self.action_candidates]:
                raise ValueError("candidate pool order or completeness mismatch")
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


class TechnicalOmissionConfirmation(StrictModel):
    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_boolean_confirmation(cls, value):
        if value is not True:
            raise ValueError("confirmation must be explicit true")
        return value

    confirmed: Literal[True]
    omitted_action_ids: list[Label] = Field(min_length=1, max_length=1000)
    selected_action_ids: list[Label] = Field(max_length=3)
    client_action_ids: list[Label] = Field(max_length=3)


class DeliveryRevision(StrictModel):
    model_config = ConfigDict(json_schema_extra={
        "allOf": [
            {"if": {"properties": {"schema_version": {"enum": ["v22_delivery_revision_v2", "v22_delivery_revision_v3"]}}},
             "then": {"required": ["client_action_ids"], "properties": {"client_action_ids": {"type": "array"}}},
             "else": {"not": {"properties": {"client_action_ids": {}}, "required": ["client_action_ids"]}}},
            {"if": {"properties": {"schema_version": {"const": "v22_delivery_revision_v3"}}},
             "else": {"not": {"properties": {"technical_omission_confirmation": {}}, "required": ["technical_omission_confirmation"]}}}
        ]
    })
    schema_version: Literal["v22_delivery_revision_v1", "v22_delivery_revision_v2", "v22_delivery_revision_v3"]
    client_action_ids: list[Label] | None = Field(default=None, max_length=3)
    technical_omission_confirmation: TechnicalOmissionConfirmation | None = None

    @model_serializer(mode="wrap")
    def preserve_v1(self, handler):
        result = handler(self)
        for key in ("client_action_ids", "technical_omission_confirmation"):
            if getattr(self, key) is None: result.pop(key, None)
        return result

    @model_validator(mode="after")
    def visibility_version(self):
        if self.schema_version in {"v22_delivery_revision_v2", "v22_delivery_revision_v3"}:
            ids = self.client_action_ids
            if ids is None or len(set(ids)) != len(ids) or not set(ids) <= {a.id for a in self.actions}:
                raise ValueError("client action selection must reference unique delivery actions")
        elif "client_action_ids" in self.model_fields_set:
            raise ValueError("client action selection requires revision v2")
        if self.schema_version != "v22_delivery_revision_v3" and "technical_omission_confirmation" in self.model_fields_set:
            raise ValueError("technical confirmation requires v3")
        if "technical_omission_confirmation" in self.model_fields_set and self.technical_omission_confirmation is None:
            raise ValueError("absent confirmation must not be null")
        return self
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
        v3 = self.schema_version == "v22_delivery_revision_v3"
        if v3 != (report.schema_version == "2.2.7"):
            raise ValueError("v12 report requires revision v3")
        if self.schema_version == "v22_delivery_revision_v2" and report.schema_version not in {"2.2.5", "2.2.6"}:
            raise ValueError("visibility edits require labelled findings")
        if (self.report_id != report.id or self.user_id != manifest.user_id or self.case_id != manifest.case_id
                or self.source_checksum != verified_request_digest(report.model_dump(mode="json"))):
            raise ValueError("delivery source binding mismatch")
        ids = [a.id for a in self.actions]
        if len(set(ids)) != len(ids) or len(ids) != len(report.actions) or (not v3 and set(ids) != {a.id for a in report.actions}):
            raise ValueError("delivery must preserve system action identities")
        originals = {a.id: a for a in (report.action_candidates if v3 else report.actions)}
        if not set(ids) <= originals.keys(): raise ValueError("unknown frozen candidate")
        if v3:
            from app.report_v22.candidate_delivery import validate_selection
            validate_selection(report, ids, self.client_action_ids,
                self.technical_omission_confirmation.model_dump(mode="json") if self.technical_omission_confirmation else None)
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
