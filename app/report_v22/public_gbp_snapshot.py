"""Build offline normalized snapshots; no provider, persistence, clock or UUID IO."""
from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.public_gbp_errors import PublicGbpError
from app.report_v22.public_gbp_identity import assess_identity
from app.report_v22.public_gbp_models import CustomerPublicGbpSnapshot, CustomerPublicGbpSnapshotInput


def build_customer_public_gbp_snapshot(value: CustomerPublicGbpSnapshotInput | dict) -> CustomerPublicGbpSnapshot:
    try:
        reject_nonfinite(value)
        raw = value.model_dump(mode="python", warnings=False) if isinstance(value, CustomerPublicGbpSnapshotInput) else value
        request = CustomerPublicGbpSnapshotInput.model_validate(raw)
        if len(canonical_json_bytes(request)) > request.limits.max_bytes:
            raise PublicGbpError("LIMIT_EXCEEDED")
        identity, reasons = assess_identity(request.reference, request)
        result = CustomerPublicGbpSnapshot(
            **request.model_dump(mode="python", exclude={"reference", "limits"}),
            subject_reference_checksum=request_digest(request.reference),
            identity_match_status=identity, identity_reasons=reasons,
            health_status="healthy" if request.collection_status == "succeeded" else "unavailable",
        )
        if len(canonical_json_bytes(result)) > request.limits.max_bytes:
            raise PublicGbpError("LIMIT_EXCEEDED")
        return result
    except (ValidationError, TypeError, ValueError):
        raise PublicGbpError("INPUT_INVALID") from None
