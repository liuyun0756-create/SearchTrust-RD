"""Explicit offline v13 input construction; never overwrites a frozen object."""
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.report_v22.business_identity_v2 import (BusinessIdentityV2, ModeConfirmation,
    OperatingLocation, OperatingPlace, IdentityMatch, IDENTITY_RULESET)
from app.report_v22.capability import AnalysisInputManifest
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.operating_model import inspect_operating_model, assess_mode


def source_set_checksum(request):
    return request_digest([{"snapshot_id": str(s.binding.snapshot_id), "payload_checksum": s.binding.payload_checksum}
        for s in request.evidence_input.sources])


def prepare_identity_input(manifest, request, *, confirmation=None):
    """Explicit caller-confirmed input. Legacy automatic modes are not confirmations.

    Phase 1 is an offline preview at the frozen evidence time. The later public
    reassessment workflow will persist a separately authorized identity version.
    """
    original = AnalysisInputManifest.model_validate_json(canonical_json_bytes(manifest))
    request = PublicFindingsInput.model_validate_json(canonical_json_bytes(request))
    old = request.business_identity
    if old is None or isinstance(old, BusinessIdentityV2):
        raise ValueError("expected legacy bound input")
    automatic, notes = inspect_operating_model(request)
    gbp = next((s for s in request.evidence_input.sources if s.kind == "public_gbp"), None)
    reference = request.evidence_input.context.customer_public_gbp
    branch = next((k.value for k in reference.entity_keys if k.kind == "cid"), None) if reference else None
    confirmed = ModeConfirmation.model_validate(confirmation) if confirmation else None
    if confirmed and confirmed.evidence_checksum != source_set_checksum(request):
        raise ValueError("confirmation evidence binding mismatch")
    assessment = automatic.model_copy(update={"value": confirmed.value, "state": "resolved", "source": "explicit_confirmation", "confirmation_ref": confirmed}) if confirmed else automatic
    fields = gbp.payload.record.fields if gbp else None
    address = fields.address.value if fields and fields.address.state == "observed" else None
    location = OperatingLocation(state="observed" if address else "not_checked",
        location=OperatingPlace(address=address, country_code=old.primary_location.country_code) if address else None,
        role="unknown", source_refs=[f"{gbp.binding.snapshot_id}:/payload/record/fields/address/value"] if address else [])
    identity = BusinessIdentityV2(schema_version="business_identity_v2", business_name=old.business_name,
        site_url=old.site_url, normalized_domain=old.normalized_domain, public_gbp_url=old.public_gbp_url,
        country_code=old.primary_location.country_code, operating_model=assessment, operating_location=location,
        selected_branch_ref=branch, identity_match=IdentityMatch(
            state="matched" if gbp and gbp.binding.identity_match_status == "matched" else "not_checked",
            matched_on=["reviewed_saved_identity"] if gbp and gbp.binding.identity_match_status == "matched" else [],
            source_refs=[str(gbp.binding.snapshot_id)] if gbp else [], rejected_candidates=[]))
    updated = request.model_copy(update={"business_identity": identity, "schema_version": "public_findings_input_v2"})
    raw = deepcopy(original.model_dump(mode="json"))
    checksum = request_digest(identity)
    raw.update(schema_version="v22_analysis_input_v3", ruleset_version=IDENTITY_RULESET,
        identity_version_id=str(uuid5(NAMESPACE_URL, f"{original.case_id}:{checksum}")), identity_checksum=checksum,
        target_market=request.evidence_input.context.target_market.model_dump(mode="json"),
        evidence_as_of=original.evaluated_at, generation_input_checksum=verified_request_digest(updated.model_dump(mode="json")))
    raw["input_checksum"] = verified_request_digest({k:v for k,v in raw.items() if k != "input_checksum"})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)), updated, automatic, notes
