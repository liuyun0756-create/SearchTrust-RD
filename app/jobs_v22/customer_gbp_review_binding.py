"""Promote only the final A3-retained acquisition to the v22 review contract."""
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from app.jobs_v22.capability_acquisition_input import stamp
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.result_persistence import result_snapshot_id
from app.report_v22.capability import AnalysisInputManifest
from app.report_v22.customer_review_sample import BASE_RULESETS, MANIFEST_SCHEMA, RULESET, SNAPSHOT_SCHEMA
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.identity_input import source_set_checksum
from app.report_v22.operating_model import inspect_operating_model
from app.report_v22.public_gbp_models import CustomerPublicGbpSnapshotV2, CustomerPublicGbpSnapshotV3


def eligible_profile(manifest, request):
    provenance = manifest.identity_inheritance
    if not provenance or provenance.branch != "retained" or manifest.content_ruleset not in BASE_RULESETS:
        return None
    source = next((source for source in request.evidence_input.sources if source.kind == "public_gbp"), None)
    return source if source and type(source.payload) is CustomerPublicGbpSnapshotV2 else None


def bind_customer_review_sample(manifest, request, sample):
    profile = eligible_profile(manifest, request)
    if not profile or sample.branch_ref != request.business_identity.selected_branch_ref:
        raise ValueError("review sample is not for the retained branch")
    payload = CustomerPublicGbpSnapshotV3.model_validate_json(canonical_json_bytes(dict(
        profile.payload.model_dump(mode="json"), schema_version=SNAPSHOT_SCHEMA,
        review_sample=sample.model_dump(mode="json"))))
    checksum = request_digest(payload)
    snapshot_id = result_snapshot_id("public_gbp", manifest.case_id, checksum)
    old_id = str(profile.binding.snapshot_id)
    new_id = str(snapshot_id)

    def rebind_reference(ref):
        if ref == old_id or ref.startswith(old_id + ":"):
            return new_id + ref[len(old_id):]
        return ref

    raw = request.model_dump(mode="json")
    for source in raw["evidence_input"]["sources"]:
        if source["kind"] == "public_gbp":
            source["payload"] = payload.model_dump(mode="json")
            source["binding"].update(snapshot_id=new_id, schema_version=SNAPSHOT_SCHEMA, payload_checksum=checksum)
    at = max(request.evidence_input.context.evaluated_at, sample.completed_at)
    raw["evidence_input"]["context"]["evaluated_at"] = stamp(at)
    for field in ("identity_match", "operating_location"):
        source_refs = raw["business_identity"][field]["source_refs"]
        raw["business_identity"][field]["source_refs"] = [rebind_reference(ref) for ref in source_refs]

    bound = PublicFindingsInput.model_validate_json(canonical_json_bytes(raw))
    confirmed = raw["business_identity"]["operating_model"]["confirmation_ref"]
    auto, _ = inspect_operating_model(bound)
    mode = auto.model_dump(mode="json")
    if confirmed:
        confirmed["evidence_checksum"] = source_set_checksum(bound)
        mode.update(value=confirmed["value"], state="resolved", source="explicit_confirmation", confirmation_ref=confirmed)
    raw["business_identity"]["operating_model"] = mode
    bound = PublicFindingsInput.model_validate_json(canonical_json_bytes(raw))
    identity_checksum = request_digest(bound.business_identity)
    result = deepcopy(manifest.model_dump(mode="json", exclude={"input_checksum"}))
    for ref in result["snapshots"]:
        if ref["source_kind"] == "public_gbp":
            ref.update(snapshot_id=new_id, schema_version=SNAPSHOT_SCHEMA, payload_checksum=checksum)
    result.update(schema_version=MANIFEST_SCHEMA, ruleset_version=RULESET, evaluated_at=stamp(at), evidence_as_of=stamp(at),
                  identity_version_id=str(uuid5(NAMESPACE_URL, f"{manifest.case_id}:{identity_checksum}")),
                  identity_checksum=identity_checksum, generation_input_checksum=verified_request_digest(bound.model_dump(mode="json")))
    result["input_checksum"] = verified_request_digest(result)
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(result)), bound
