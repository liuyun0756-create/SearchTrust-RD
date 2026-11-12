"""Pure saved-input reassessment. No provider, clock, database or collection IO."""
from copy import deepcopy
from uuid import UUID, uuid5

from pydantic import Field, field_validator, model_validator

from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.report_v22.business_identity_v2 import BusinessIdentityV2, ModeConfirmation, OperatingMode, OperatingLocation, OperatingPlace, LocationConfirmation
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport, instant
from app.report_v22.capability_public import build_public_capability_report, _bound_public_sources
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.identity_input import prepare_identity_input, source_set_checksum
from app.report_v22.models import StrictModel
from app.report_v22.operating_model import inspect_operating_model


class IdentitySelection(StrictModel):
    operating_model: OperatingMode
    branch_ref: str = Field(min_length=1, max_length=320)
    reason: str = Field(min_length=1, max_length=1000)
    customers_can_visit: bool = False

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("confirmation reason is required")
        return value.strip()

    @model_validator(mode="after")
    def visit_requires_storefront(self):
        if self.customers_can_visit and self.operating_model not in {"storefront", "hybrid"}:
            raise ValueError("customer access confirmation requires storefront or hybrid")
        return self


def validated_base(report, request):
    base = CapabilityReport.model_validate_json(canonical_json_bytes(report))
    value = PublicFindingsInput.model_validate_json(canonical_json_bytes(request))
    if base.report_type != "prospect" or base.input_manifest.mode != "acquisition":
        raise ValueError("PUBLIC_BASE_REQUIRED")
    if verified_request_digest(value.model_dump(mode="json")) != base.input_manifest.generation_input_checksum:
        raise ValueError("BASE_INPUT_MISMATCH")
    _bound_public_sources(base.input_manifest, value)
    return base, value


def identity_workspace(report, request):
    base, value = validated_base(report, request)
    if isinstance(value.business_identity, BusinessIdentityV2):
        identity = value.business_identity
    else:
        _, converted, _, _ = prepare_identity_input(base.input_manifest, value)
        identity = converted.business_identity
    automatic, notes = inspect_operating_model(value)
    return identity, automatic, notes


def reassess_public_report(report, request, selection, *, request_id: UUID, evaluated_at: str):
    """Only a validated stored base may be supplied by the authorized service.

    Different branch details need their own saved input; a search candidate is
    insufficient to replace a full frozen GBP source. This fails without writes.
    """
    base, value = validated_base(report, request)
    selection = IdentitySelection.model_validate(selection)
    original_bytes = canonical_json_bytes(value.evidence_input)
    identity, _, _ = identity_workspace(base, value)
    reference = value.evidence_input.context.customer_public_gbp
    key = selection.branch_ref.split(":", 1)
    if len(key) != 2 or not reference or not any(k.kind == key[0] and k.value == key[1] for k in reference.entity_keys):
        raise ValueError("SAVED_BRANCH_DETAILS_REQUIRED")
    if instant(evaluated_at) < instant(base.generated_at):
        raise ValueError("REASSESSMENT_TIME_INVALID")
    confirmation = ModeConfirmation(value=selection.operating_model, confirmed_by=str(base.input_manifest.user_id),
        confirmed_at=evaluated_at, reason=selection.reason, normalized_domain=identity.normalized_domain,
        selected_branch_ref=key[1], evidence_checksum=source_set_checksum(value),
        source_ref=f"identity-review:{request_id}")
    # Resolve the address from the selected frozen GBP details, never from a
    # browser field or a prior review. Each review needs a fresh explicit tick.
    gbp = next((s for s in value.evidence_input.sources if s.kind == "public_gbp"), None)
    field = gbp.payload.record.fields.address if gbp else None
    address = field.value if field and field.state == "observed" else None
    location = OperatingLocation(state="observed" if address else "not_checked",
        location=OperatingPlace(address=address, country_code=identity.country_code) if address else None,
        role="unknown", source_refs=[f"{gbp.binding.snapshot_id}:/payload/record/fields/address/value"] if address else [])
    if identity.operating_location.location and identity.operating_location.location.address == address:
        location = identity.operating_location.model_copy(update=dict(role="unknown", state="observed", confirmation_ref=None))
    if selection.customers_can_visit:
        if not address:
            raise ValueError("SAVED_BRANCH_ADDRESS_REQUIRED")
        location = location.model_copy(update=dict(state="seo_confirmed", role="customer_facing",
            confirmation_ref=LocationConfirmation(confirmed_by=str(base.input_manifest.user_id),
                confirmed_at=evaluated_at, selected_branch_ref=key[1], address=address,
                source_ref=f"identity-review:{request_id}")))
    automatic, _ = inspect_operating_model(value)
    updated_identity = identity.model_copy(update={"selected_branch_ref": key[1], "operating_location": location, "operating_model":
        automatic.model_copy(update={"value": selection.operating_model, "state": "resolved",
            "source": "explicit_confirmation", "confirmation_ref": confirmation})})
    updated = value.model_copy(update={"schema_version": "public_findings_input_v2", "business_identity": updated_identity})
    raw = deepcopy(base.input_manifest.model_dump(mode="json"))
    keyword = base.input_manifest.ruleset_version == "v22_capability_public_v14"
    raw.update(schema_version="v22_analysis_input_v4" if keyword else "v22_analysis_input_v3",
        ruleset_version="v22_capability_public_v14" if keyword else "v22_capability_public_v13",
        id=str(uuid5(request_id, "manifest")), job_id=str(uuid5(request_id, "report")),
        identity_version_id=str(uuid5(request_id, "identity")), identity_checksum=request_digest(updated_identity),
        target_market=value.evidence_input.context.target_market.model_dump(mode="json"),
        execution_type="public_reassessment", evidence_as_of=base.input_manifest.evidence_as_of or base.input_manifest.evaluated_at,
        evaluated_at=evaluated_at, generation_input_checksum=verified_request_digest(updated.model_dump(mode="json")),
        previous_report=None, implementation=None,
        base_report_ref=dict(report_id=str(base.id), case_id=str(base.input_manifest.case_id),
            report_type=base.report_type, version=base.version, payload_checksum=verified_request_digest(base.model_dump(mode="json"))))
    raw['input_checksum'] = verified_request_digest({k: v for k,v in raw.items() if k != 'input_checksum'})
    manifest = AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw))
    after = build_public_capability_report(manifest, updated)
    # Compare to the actual base report, including legacy rules. A rule upgrade
    # must not silently hide unrelated differences behind an intermediate replay.
    baseline = base
    allowed = {"v22_public.gbp_address_alignment", "v22_public.gbp_service_area_alignment"}
    def unrelated(r):
        return sorted((f.rule_id, f.target, f.observed_fact, f.judgment, f.confidence)
                      for f in r.findings if f.rule_id not in allowed and f.rule_id != 'v22_capability.public_assessment')
    if unrelated(baseline) != unrelated(after):
        raise ValueError("UNEXPLAINED_FACT_CHANGE")
    if canonical_json_bytes(updated.evidence_input) != original_bytes:
        raise ValueError("FROZEN_SOURCE_MUTATION")
    before_actions = {a.id for a in base.action_candidates or base.actions}
    after_actions = {a.id for a in after.action_candidates or after.actions}
    diff = dict(before=base.contact_checks or [], after=after.contact_checks or [],
        removed_action_ids=sorted(before_actions-after_actions), added_action_ids=sorted(after_actions-before_actions),
        removed_actions=[dict(title=a.title,target=a.target) for a in base.action_candidates or base.actions if a.id not in after_actions],
        added_actions=[dict(title=a.title,target=a.target) for a in after.action_candidates or after.actions if a.id not in before_actions],
        copy_migrated=False, requires_approval=True, evidence_as_of=manifest.evidence_as_of,
        evaluated_at=manifest.evaluated_at)
    return after, updated, diff
