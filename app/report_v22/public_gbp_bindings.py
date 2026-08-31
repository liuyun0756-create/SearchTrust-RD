"""Re-derive identities at the evidence boundary; never trust saved labels."""
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.public_gbp_errors import PublicGbpError
from app.report_v22.public_gbp_models import PublicGbpSample
from app.report_v22.public_gbp_snapshot import build_customer_public_gbp_snapshot


def require(condition):
    if not condition:
        raise EvidenceError("SNAPSHOT_BINDING_INVALID")


def validate_reference(context):
    reference = context.customer_public_gbp
    if reference is not None:
        require(context.report_type == "prospect")
        require(reference.case_id == context.case_id and reference.site_url == context.site_url)
        require(reference.confirmed_at <= context.evaluated_at)
        if len(canonical_json_bytes(reference)) > 1_000_000:
            raise EvidenceError("LIMIT_EXCEEDED")


def validate_public_binding(source, context):
    reference, payload, binding = context.customer_public_gbp, source.payload, source.binding
    require(context.report_type == "prospect" and reference is not None)
    if len(canonical_json_bytes(payload)) > 1_000_000:
        raise EvidenceError("LIMIT_EXCEEDED")
    require(payload.subject_reference_checksum == request_digest(reference))
    require(binding.expires_at == payload.expires_at and binding.health_status == payload.health_status
            and binding.identity_match_status == payload.identity_match_status)
    try:
        raw = payload.model_dump(mode="python", include=set(PublicGbpSample.model_fields))
        rebuilt = build_customer_public_gbp_snapshot({**raw, "reference": reference})
    except PublicGbpError as exc:
        kind = "LIMIT_EXCEEDED" if exc.error_code.endswith("LIMIT_EXCEEDED") else "SNAPSHOT_BINDING_INVALID"
        raise EvidenceError(kind) from None
    require(canonical_json_bytes(payload) == canonical_json_bytes(rebuilt))


def source_origin(source):
    if source.kind == "public_gbp":
        return "public_profile"
    if source.kind == "first_party" and source.binding.source_type == "gbp":
        return "first_party"
    return None


def conflicts_with_missing(source, missing):
    if source.binding.source_type != missing.source_type:
        return False
    if missing.source_type == "gbp":
        return source_origin(source) == (missing.gbp_origin or "first_party")
    return True
